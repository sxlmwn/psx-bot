"""
Tests for Real Portfolio Plans, Graduation Metrics, and Journal Post-Mortems.
"""

from datetime import datetime, time, timezone
import pytest

from veterandesk.portfolio.manager import PortfolioManager, PortfolioAction
from veterandesk.execution.graduation import compute_performance_metrics
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker
from veterandesk.journal.lessons import LessonsMemory
from veterandesk.journal.post_mortem import PostMortemEngine, TradeVerdict
from veterandesk.strategy.models import SignalAction, TradeSignal


class TestPortfolioAndJournal:
    def test_mandatory_stop_loss_on_portfolio_plan(self):
        mgr = PortfolioManager(total_portfolio_equity=1000000.0)

        # 1. Stop loss is None or <= 0 -> Raises ValueError
        with pytest.raises(ValueError, match="Position cannot be saved without a stop loss"):
            mgr.create_position_plan(
                ticker="ENGRO",
                quantity=500,
                entry_price=320.0,
                stop_loss=0.0
            )

        # 2. Stop loss >= entry price -> Raises ValueError
        with pytest.raises(ValueError, match="Stop loss must be lower than entry price"):
            mgr.create_position_plan(
                ticker="ENGRO",
                quantity=500,
                entry_price=320.0,
                stop_loss=325.0
            )

        # 3. Valid plan
        plan = mgr.create_position_plan(
            ticker="ENGRO",
            quantity=500,
            entry_price=320.0,
            stop_loss=305.0,
            target_price=350.0
        )
        assert plan.ticker == "ENGRO"
        assert plan.total_rupee_risk == 500 * (320.0 - 305.0)  # 7,500 PKR

    def test_session_call_recommendations(self):
        mgr = PortfolioManager(total_portfolio_equity=1000000.0)
        mgr.create_position_plan(
            ticker="LUCK",
            quantity=200,
            entry_price=500.0,
            stop_loss=480.0,
            target_price=540.0,
            trim_level=520.0
        )

        # Stop Hit -> EXIT
        action_stop, msg_stop = mgr.evaluate_session_call("LUCK", current_price=479.0)
        assert action_stop == PortfolioAction.EXIT
        assert "STOP HIT" in msg_stop

        # Target Hit -> EXIT
        action_tgt, msg_tgt = mgr.evaluate_session_call("LUCK", current_price=542.0)
        assert action_tgt == PortfolioAction.EXIT
        assert "TARGET REACHED" in msg_tgt

        # Trim Hit -> TRIM
        action_trim, msg_trim = mgr.evaluate_session_call("LUCK", current_price=522.0)
        assert action_trim == PortfolioAction.TRIM
        assert "TRIM LEVEL REACHED" in msg_trim

        # Within bounds -> HOLD
        action_hold, msg_hold = mgr.evaluate_session_call("LUCK", current_price=505.0)
        assert action_hold == PortfolioAction.HOLD
        assert "HOLD" in msg_hold

    def test_graduation_criteria_evaluation(self):
        # Case 1: Zero trades -> Not graduated
        m0 = compute_performance_metrics(closed_trades=[], starting_balance=500000.0)
        assert m0.is_graduated is False
        assert any("Zero closed trades" in b for b in m0.graduation_blockers)

        # Case 2: 30 winning trades, positive expectancy, 0 drawdown, 0 violations -> Graduated!
        trades = []
        for i in range(30):
            t = DemoTrade(
                trade_id=f"T_{i}",
                signal_id=f"S_{i}",
                ticker="OGDC",
                action=SignalAction.BUY,
                shares=100,
                entry_price=100.0,
                stop_loss=95.0,
                target_price=110.0,
                slippage_pct=0.002,
                filled_entry_price=100.20,
            )
            t.filled_exit_price = 108.0
            t.net_pnl = 750.0  # Profit
            trades.append(t)

        m_grad = compute_performance_metrics(trades, starting_balance=500000.0, recent_violations_count=0)
        assert m_grad.total_trades == 30
        assert m_grad.win_rate_pct == 100.0
        assert m_grad.expectancy_pkr > 0
        assert m_grad.max_drawdown_pct == 0.0
        assert m_grad.is_graduated is True
        assert len(m_grad.graduation_blockers) == 0

    @pytest.mark.asyncio
    async def test_post_mortem_and_lesson_injection(self):
        lessons_mem = LessonsMemory()
        engine = PostMortemEngine(lessons_memory=lessons_mem)

        trade = DemoTrade(
            trade_id="TRD_POST_1",
            signal_id="SIG_1",
            ticker="OGDC",
            action=SignalAction.BUY,
            shares=500,
            entry_price=140.0,
            stop_loss=135.0,
            target_price=147.5,
            slippage_pct=0.002,
            filled_entry_price=140.28,
        )
        trade.filled_exit_price = 147.5
        trade.exit_reason = ExitReason.TARGET_HIT
        trade.net_pnl = 3500.0

        engine.queue_trade_for_post_mortem(trade)
        assert len(engine.pending_queue) == 1

        processed = await engine.process_pending_queue()
        assert processed == 1
        assert len(engine.pending_queue) == 0

        record = engine.completed_journal["TRD_POST_1"]
        assert record.verdict == TradeVerdict.RIGHT
        assert record.status.value == "COMPLETED"
        assert record.transferable_lesson is not None

        # Verify lesson in memory
        active_lessons = lessons_mem.get_active_lessons()
        assert len(active_lessons) == 1
        assert "OGDC" in active_lessons[0].category

        # Verify prompt injection text
        prompt_context = lessons_mem.build_pre_session_prompt_context()
        assert "VETERANDESK ACTIVE LESSONS MEMORY" in prompt_context
        assert active_lessons[0].times_cited == 1

    @pytest.mark.asyncio
    async def test_post_mortem_loss_with_target_hit_cannot_be_right_or_positive_expectancy(self):
        """
        Verify that a trade hitting target nominally but suffering a net loss due to
        friction is classified as 'Right-for-wrong-reason' and NEVER claims positive expectancy.
        """
        lessons_mem = LessonsMemory()
        engine = PostMortemEngine(lessons_memory=lessons_mem)

        trade = DemoTrade(
            trade_id="TRD_FRICTION_LOSS_1",
            signal_id="SIG_FRICTION_1",
            ticker="OGDC",
            action=SignalAction.BUY,
            shares=1506,
            entry_price=328.48,
            stop_loss=327.00,
            target_price=329.98,
            slippage_pct=0.002,
            filled_entry_price=329.14,
            exit_price=329.98,
            filled_exit_price=329.32,
            exit_reason=ExitReason.TARGET_HIT,
            gross_pnl=271.08,
            entry_fees=768.31,
            exit_fees=768.73,
            net_pnl=-1265.96,
        )

        record = engine.queue_trade_for_post_mortem(trade)
        assert record.net_pnl == -1265.96
        assert record.exit_reason == "TARGET_HIT"

        processed = await engine.process_pending_queue()
        assert processed == 1

        completed = engine.completed_journal["TRD_FRICTION_LOSS_1"]
        # Non-negotiable: MUST NOT be 'Right'
        assert completed.verdict == TradeVerdict.RIGHT_FOR_WRONG_REASON
        assert completed.verdict != TradeVerdict.RIGHT
        # Non-negotiable: Analysis must highlight round-trip friction and net loss
        assert "friction" in completed.post_mortem_analysis.lower() or "transaction" in completed.post_mortem_analysis.lower()
        # Non-negotiable: Must NEVER claim positive expectancy on a net loss!
        assert "positive expectancy" not in completed.transferable_lesson.lower()

    def test_exit_condition_validation_and_evaluation(self):
        """
        Verify PaperBroker exit condition evaluation and strict validation in execute_exit.
        """
        ledger = DoubleEntryLedger(starting_balance_pkr=1000000.0)
        broker = PaperBroker(ledger=ledger, persist_to_db=False)

        sig = TradeSignal(
            signal_id="SIG_EXIT_TEST",
            ticker="OGDC",
            entry_price=328.48,
            stop_loss=327.00,
            target_price=329.98,
            reward_risk_ratio=1.01,
            position_size=100,
            confidence_pct=75,
            invalidation_reason="Test",
            created_at=datetime.now(timezone.utc),
            session_id="exit_test_session",
        )

        trade = broker.execute_buy(signal=sig, shares=100, scraped_price=328.48)

        # 1. evaluate_exit_condition
        # Price below target and above stop -> None
        assert broker.evaluate_exit_condition(trade, scraped_price=328.50) is None
        # Price reaches target -> TARGET_HIT
        assert broker.evaluate_exit_condition(trade, scraped_price=329.98) == ExitReason.TARGET_HIT
        assert broker.evaluate_exit_condition(trade, scraped_price=330.50) == ExitReason.TARGET_HIT
        # Price reaches or drops below stop -> STOP_HIT
        assert broker.evaluate_exit_condition(trade, scraped_price=327.00) == ExitReason.STOP_HIT
        assert broker.evaluate_exit_condition(trade, scraped_price=326.50) == ExitReason.STOP_HIT
        # Cutoff time >= 15:20 PKT -> TIME_STOP_1520 takes priority
        assert broker.evaluate_exit_condition(trade, scraped_price=328.50, current_time_pkt=time(15, 20)) == ExitReason.TIME_STOP_1520
        assert broker.evaluate_exit_condition(trade, scraped_price=328.50, current_time_pkt=time(15, 25)) == ExitReason.TIME_STOP_1520

        # 2. Strict validation in execute_exit
        # Attempting TARGET_HIT when market price is below target -> ValueError
        with pytest.raises(ValueError, match="Cannot exit with TARGET_HIT: market price PKR 329.32 < target price PKR 329.98"):
            broker.execute_exit(
                trade_id=trade.trade_id,
                scraped_price=329.32,
                exit_reason=ExitReason.TARGET_HIT,
            )

        # Attempting STOP_HIT when market price is above stop -> ValueError
        with pytest.raises(ValueError, match="Cannot exit with STOP_HIT: market price PKR 328.00 > stop loss PKR 327.00"):
            broker.execute_exit(
                trade_id=trade.trade_id,
                scraped_price=328.00,
                exit_reason=ExitReason.STOP_HIT,
            )

        # Valid TARGET_HIT exit at market price 329.98
        closed_trade = broker.execute_exit(
            trade_id=trade.trade_id,
            scraped_price=329.98,
            exit_reason=ExitReason.TARGET_HIT,
        )
        assert closed_trade.exit_price == 329.98
        assert closed_trade.filled_exit_price == 329.32
        assert closed_trade.exit_reason == ExitReason.TARGET_HIT

