"""
Tests for System Health Monitor and Telegram Service.

Covers:
1. Notifier unit tests with mocked Telegram API (success, failure, 3x retry exhaustion).
2. Schema validation tests ensuring zero None or blank fields in outbound messages.
3. Delivery tracking and persistence in telegram_delivery_log (pending -> sent / failed).
4. All system hook points:
   - Signal generation alert
   - Risk engine daily halt alert
   - Ledger exit / level hit alert
   - Mistake detector audit alert & discrepancy alert
   - Demo account graduation status alert
   - Health monitor silence & degradation alert
   - APScheduler daily brief & session summary jobs
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import httpx

from veterandesk.alerts.scheduler import run_daily_brief_job, run_session_summary_job, create_alert_scheduler
from veterandesk.alerts.telegram import (
    DeliveryStatus,
    MessageType,
    OutboundMessage,
    TelegramService,
    get_delivery_stats,
    telegram_service,
)
from veterandesk.audit.mistake_detector import MistakeDetector
from veterandesk.execution.graduation import (
    PerformanceMetrics,
    compute_performance_metrics,
    notify_graduation_status,
)
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker, TradeStatus
from veterandesk.health.monitor import ComponentStatus, SystemHealthMonitor
from veterandesk.risk.engine import RiskEngine
from veterandesk.strategy.models import SignalAction, SignalStatus, TradeSignal
from veterandesk.strategy.orb import compute_orb_signal


class TestHealthAndAlerts:
    def test_health_monitor_heartbeat_and_down_detection(self) -> None:
        ledger = DoubleEntryLedger(starting_balance_pkr=500000.0)
        monitor = SystemHealthMonitor(ledger=ledger)

        # Immediate check -> not down
        assert monitor.is_system_down(threshold_seconds=120) is False

        # Run heartbeat
        statuses = monitor.run_heartbeat()
        assert "ledger" in statuses
        assert statuses["ledger"].status == ComponentStatus.GREEN

        # Component status update
        monitor.record_check("scraper", ComponentStatus.RED, latency_ms=1200.0, message="Timeout")
        assert monitor.components["scraper"].status == ComponentStatus.RED

    def test_health_monitor_silence_alert_trigger(self) -> None:
        monitor = SystemHealthMonitor()
        # Simulate last heartbeat in the past > 120s
        monitor.last_heartbeat = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert monitor.is_system_down(120) is True

        with patch.object(telegram_service, "send_system_health_alert", return_value=True) as mock_alert:
            triggered = monitor.check_health_and_alert(threshold_seconds=120)
            assert triggered is True
            mock_alert.assert_called_once()
            args, kwargs = mock_alert.call_args
            assert kwargs["status"] == "SYSTEM_DOWN"

    @pytest.mark.asyncio
    async def test_telegram_mock_api_success_sync_and_async(self) -> None:
        svc = TelegramService(bot_token="test_token", chat_id="12345678", enabled=True)
        svc.min_interval_seconds = 0.0

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.text = '{"ok": true}'

        # Test sync delivery success
        with patch("httpx.Client.post", return_value=mock_resp):
            msg_sync = svc.enqueue_message(MessageType.SIGNAL, "Test Sync Message")
            success_sync = svc._send_with_retry_sync(msg_sync)
            assert success_sync is True
            assert msg_sync.status == DeliveryStatus.SENT
            assert msg_sync.attempts == 1
            assert msg_sync.is_delivered is True
            assert msg_sync in svc.delivered_history

        # Test async delivery success
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_async_post:
            mock_async_post.return_value = mock_resp
            msg_async = svc.enqueue_message(MessageType.ALERT, "Test Async Message")
            success_async = await svc._send_with_retry_async(msg_async)
            assert success_async is True
            assert msg_async.status == DeliveryStatus.SENT
            assert msg_async.attempts == 1
            assert msg_async.is_delivered is True

    def test_telegram_mock_api_retry_exhausted_failure_sync(self) -> None:
        svc = TelegramService(bot_token="test_token", chat_id="12345678", enabled=True)
        svc.min_interval_seconds = 0.0

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("httpx.Client.post", return_value=mock_resp), patch("time.sleep"):
            msg = svc.enqueue_message(MessageType.SIGNAL, "Test Failure Message", reference_id="REF_FAIL_1")
            success = svc._send_with_retry_sync(msg)
            assert success is False
            assert msg.status == DeliveryStatus.FAILED
            assert msg.attempts == 3
            assert msg.is_delivered is False
            assert "HTTP 500" in (msg.last_error or "")
            assert msg in svc.failed_dead_letter

    @pytest.mark.asyncio
    async def test_telegram_mock_api_retry_exhausted_failure_async(self) -> None:
        svc = TelegramService(bot_token="test_token", chat_id="12345678", enabled=True)
        svc.min_interval_seconds = 0.0

        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectTimeout("Timed out")), patch("asyncio.sleep"):
            msg = svc.enqueue_message(MessageType.ALERT, "Test Timeout Async", reference_id="REF_FAIL_ASYNC")
            success = await svc._send_with_retry_async(msg)
            assert success is False
            assert msg.status == DeliveryStatus.FAILED
            assert msg.attempts == 3
            assert "Timed out" in (msg.last_error or "")
            assert msg in svc.failed_dead_letter

    @pytest.mark.asyncio
    async def test_telegram_disabled_or_unconfigured_marks_skipped_not_sent(self) -> None:
        """
        Critical regression test:
        Asserts that when the service is disabled or unconfigured, messages
        are recorded with status SKIPPED, NEVER SENT, and is_delivered is False.
        """
        # Case 1: Disabled flag (enabled=False) - Sync
        svc_disabled = TelegramService(bot_token="tok123", chat_id="chat123", enabled=False)
        msg1 = svc_disabled.enqueue_message(MessageType.ALERT, "Test Disabled Alert")
        res1 = svc_disabled._send_with_retry_sync(msg1)
        assert res1 is False
        assert msg1.status == DeliveryStatus.SKIPPED
        assert msg1.status != DeliveryStatus.SENT
        assert msg1.is_delivered is False
        assert msg1.attempts == 0
        assert msg1.sent_at is None
        assert msg1 in svc_disabled.skipped_history
        assert msg1 not in svc_disabled.delivered_history

        # Case 2: Missing bot_token - Sync
        svc_no_tok = TelegramService(bot_token="", chat_id="chat123", enabled=True)
        msg2 = svc_no_tok.enqueue_message(MessageType.SIGNAL, "Test No Token Signal")
        res2 = svc_no_tok._send_with_retry_sync(msg2)
        assert res2 is False
        assert msg2.status == DeliveryStatus.SKIPPED
        assert msg2.status != DeliveryStatus.SENT
        assert msg2.is_delivered is False
        assert msg2.attempts == 0
        assert msg2.sent_at is None

        # Case 3: Missing chat_id - Sync
        svc_no_chat = TelegramService(bot_token="tok123", chat_id="", enabled=True)
        msg3 = svc_no_chat.enqueue_message(MessageType.LEVEL_HIT, "Test No Chat Level Hit")
        res3 = svc_no_chat._send_with_retry_sync(msg3)
        assert res3 is False
        assert msg3.status == DeliveryStatus.SKIPPED
        assert msg3.status != DeliveryStatus.SENT
        assert msg3.is_delivered is False

        # Case 4: Disabled flag - Async delivery
        msg4 = svc_disabled.enqueue_message(MessageType.ALERT, "Test Disabled Async")
        res4 = await svc_disabled._send_with_retry_async(msg4)
        assert res4 is False
        assert msg4.status == DeliveryStatus.SKIPPED
        assert msg4.status != DeliveryStatus.SENT
        assert msg4.is_delivered is False
        assert msg4.attempts == 0
        assert msg4.sent_at is None
        assert msg4 in svc_disabled.skipped_history
        assert msg4 not in svc_disabled.delivered_history


class TestSchemaValidation:
    def test_format_signal_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        sig = TradeSignal(
            signal_id="SIG_VAL_1",
            ticker="OGDC",
            entry_price=142.5,
            stop_loss=139.8,
            target_price=146.55,
            reward_risk_ratio=1.5,
            position_size=1000,
            confidence_pct=65,
            invalidation_reason="Invalidated",
            created_at=datetime.now(timezone.utc),
            session_id="sess_1",
        )

        valid_text = svc.format_signal_message(sig, shares=1000, reason_lines="ORB Breakout")
        assert "OGDC" in valid_text
        assert "PKR 142.50" in valid_text

        # Invalid cases: blank ticker, negative entry, 0 shares, blank reason
        sig_bad_ticker = TradeSignal(
            signal_id="SIG_2", ticker="", entry_price=100.0, stop_loss=90.0,
            target_price=120.0, reward_risk_ratio=2.0, position_size=100,
            confidence_pct=50, invalidation_reason="Inv", created_at=datetime.now(timezone.utc), session_id="s"
        )
        with pytest.raises(ValueError, match="ticker cannot be empty"):
            svc.format_signal_message(sig_bad_ticker, shares=100, reason_lines="Valid")

        with pytest.raises(ValueError, match="shares count invalid"):
            svc.format_signal_message(sig, shares=0, reason_lines="Valid")

        with pytest.raises(ValueError, match="reason_lines cannot be empty"):
            svc.format_signal_message(sig, shares=100, reason_lines="   ")

    def test_format_level_hit_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        valid = svc.format_level_hit_message(
            ticker="LUCK",
            trade_id="TRD_100",
            level_type="TARGET_HIT",
            price=850.0,
            fill_price=849.5,
            net_pnl=12500.50,
            closed_at_str="2026-09-06 11:30:00 UTC",
        )
        assert "LUCK" in valid
        assert "TARGET_HIT" in valid
        assert "+12,500.50" in valid

        with pytest.raises(ValueError, match="Ticker cannot be blank"):
            svc.format_level_hit_message("", "TRD_1", "STOP_HIT", 100.0, 99.0, -100.0, "time")

        with pytest.raises(ValueError, match="Trigger price invalid"):
            svc.format_level_hit_message("LUCK", "TRD_1", "STOP_HIT", -10.0, 99.0, -100.0, "time")

    def test_format_daily_loss_halt_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        valid = svc.format_daily_loss_halt_message(
            loss_pct=2.15,
            max_loss_pct=2.00,
            loss_amount_pkr=10750.0,
            halt_time_pkt="13:45:00 PKT",
            action_taken="Trading halted for day",
        )
        assert "2.15%" in valid
        assert "10,750.00" in valid

        with pytest.raises(ValueError, match="Loss pct invalid"):
            svc.format_daily_loss_halt_message(-1.0, 2.0, 1000.0, "time", "action")

        with pytest.raises(ValueError, match="Halt time cannot be blank"):
            svc.format_daily_loss_halt_message(2.0, 2.0, 10000.0, "", "action")

    def test_format_mistake_alert_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        valid = svc.format_mistake_alert_message(
            rule_violated="OVERSIZE_RISK",
            severity="CRITICAL",
            trade_id="TRD_99",
            details="Trade exceeded 1.0% risk cap",
            detected_at_str="2026-09-06 10:00:00 UTC",
        )
        assert "OVERSIZE_RISK" in valid
        assert "CRITICAL" in valid

        with pytest.raises(ValueError, match="Rule violated cannot be blank"):
            svc.format_mistake_alert_message("", "CRITICAL", "TRD_1", "details", "time")

    def test_format_graduation_status_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        valid = svc.format_graduation_status_message(
            status="GRADUATED",
            total_trades=35,
            win_rate_pct=62.5,
            expectancy_pkr=2450.0,
            max_drawdown_pct=4.8,
            blockers_or_status="Criteria met",
        )
        assert "GRADUATED" in valid
        assert "35" in valid
        # Test delivered_at property
        msg = OutboundMessage(id="M1", msg_type=MessageType.ALERT, text="Test")
        now_dt = datetime.now(timezone.utc)
        msg.delivered_at = now_dt
        assert msg.delivered_at == now_dt

        # Test send_message empty text validation
        with pytest.raises(ValueError, match="Message text cannot be empty"):
            svc.send_message("")

        # Additional format_signal_message branches
        sig_bad_entry = TradeSignal.model_construct(
            signal_id="S_BE", ticker="OGDC", entry_price=-1.0, stop_loss=90.0,
            target_price=120.0, reward_risk_ratio=2.0, position_size=100,
            confidence_pct=50, invalidation_reason="Inv", created_at=datetime.now(timezone.utc), session_id="s"
        )
        with pytest.raises(ValueError, match="entry_price invalid"):
            svc.format_signal_message(sig_bad_entry, 100, "reason")

        sig_bad_stop = TradeSignal.model_construct(
            signal_id="S_BS", ticker="OGDC", entry_price=100.0, stop_loss=-1.0,
            target_price=120.0, reward_risk_ratio=2.0, position_size=100,
            confidence_pct=50, invalidation_reason="Inv", created_at=datetime.now(timezone.utc), session_id="s"
        )
        with pytest.raises(ValueError, match="stop_loss invalid"):
            svc.format_signal_message(sig_bad_stop, 100, "reason")

        sig_bad_target = TradeSignal.model_construct(
            signal_id="S_BT", ticker="OGDC", entry_price=100.0, stop_loss=90.0,
            target_price=-1.0, reward_risk_ratio=2.0, position_size=100,
            confidence_pct=50, invalidation_reason="Inv", created_at=datetime.now(timezone.utc), session_id="s"
        )
        with pytest.raises(ValueError, match="target_price invalid"):
            svc.format_signal_message(sig_bad_target, 100, "reason")

        # Additional format_level_hit_message branches
        with pytest.raises(ValueError, match="Trade ID cannot be blank"):
            svc.format_level_hit_message("OGDC", "", "TARGET_HIT", 100.0, 100.0, 10.0, "time")
        with pytest.raises(ValueError, match="Level type cannot be blank"):
            svc.format_level_hit_message("OGDC", "T1", "", 100.0, 100.0, 10.0, "time")
        with pytest.raises(ValueError, match="Fill price invalid"):
            svc.format_level_hit_message("OGDC", "T1", "TARGET_HIT", 100.0, -5.0, 10.0, "time")
        with pytest.raises(ValueError, match="Net PnL cannot be None"):
            svc.format_level_hit_message("OGDC", "T1", "TARGET_HIT", 100.0, 100.0, None, "time")  # type: ignore
        with pytest.raises(ValueError, match="Closed at timestamp cannot be blank"):
            svc.format_level_hit_message("OGDC", "T1", "TARGET_HIT", 100.0, 100.0, 10.0, "")

        # Additional format_daily_loss_halt_message branches
        with pytest.raises(ValueError, match="Max loss pct invalid"):
            svc.format_daily_loss_halt_message(2.0, -1.0, 1000.0, "time", "action")
        with pytest.raises(ValueError, match="Loss amount invalid"):
            svc.format_daily_loss_halt_message(2.0, 2.0, -100.0, "time", "action")
        with pytest.raises(ValueError, match="Action taken cannot be blank"):
            svc.format_daily_loss_halt_message(2.0, 2.0, 1000.0, "time", "")

        # Additional format_mistake_alert_message branches
        with pytest.raises(ValueError, match="Severity cannot be blank"):
            svc.format_mistake_alert_message("RULE", "", "T1", "det", "time")
        with pytest.raises(ValueError, match="Details cannot be blank"):
            svc.format_mistake_alert_message("RULE", "WARN", "T1", "", "time")
        with pytest.raises(ValueError, match="Detected at timestamp cannot be blank"):
            svc.format_mistake_alert_message("RULE", "WARN", "T1", "det", "")

        # Additional format_graduation_status_message branches
        with pytest.raises(ValueError, match="Total trades cannot be None or negative"):
            svc.format_graduation_status_message("STAT", -1, 50.0, 10.0, 2.0, "block")
        with pytest.raises(ValueError, match="Win rate cannot be None or negative"):
            svc.format_graduation_status_message("STAT", 10, -5.0, 10.0, 2.0, "block")
        with pytest.raises(ValueError, match="Expectancy cannot be None"):
            svc.format_graduation_status_message("STAT", 10, 50.0, None, 2.0, "block")  # type: ignore
        with pytest.raises(ValueError, match="Max drawdown cannot be None or negative"):
            svc.format_graduation_status_message("STAT", 10, 50.0, 10.0, -2.0, "block")
        with pytest.raises(ValueError, match="Blockers/status description cannot be blank"):
            svc.format_graduation_status_message("STAT", 10, 50.0, 10.0, 2.0, "")

        # Additional format_system_health_alert_message branches
        with pytest.raises(ValueError, match="Reason cannot be blank"):
            svc.format_system_health_alert_message("DOWN", "", ["comp"], "time")
        with pytest.raises(ValueError, match="Timestamp cannot be blank"):
            svc.format_system_health_alert_message("DOWN", "reas", ["comp"], "")

        # Additional format_daily_brief branches
        with pytest.raises(ValueError, match="Date string cannot be blank"):
            svc.format_daily_brief("", "overview", [])
        with pytest.raises(ValueError, match="Market overview cannot be blank"):
            svc.format_daily_brief("date", "", [])

        # Additional format_session_summary branches
        with pytest.raises(ValueError, match="Session date cannot be blank"):
            svc.format_session_summary("", 1, 1, 0, 10.0, 1.0, 9.0)
        with pytest.raises(ValueError, match="Trades count cannot be None or negative"):
            svc.format_session_summary("date", -1, 1, 0, 10.0, 1.0, 9.0)
        with pytest.raises(ValueError, match="Winning trades cannot be None or negative"):
            svc.format_session_summary("date", 1, -1, 0, 10.0, 1.0, 9.0)
        with pytest.raises(ValueError, match="Losing trades cannot be None or negative"):
            svc.format_session_summary("date", 1, 1, -1, 10.0, 1.0, 9.0)
        with pytest.raises(ValueError, match="Gross PnL cannot be None"):
            svc.format_session_summary("date", 1, 1, 0, None, 1.0, 9.0)  # type: ignore
        with pytest.raises(ValueError, match="Total fees cannot be None or negative"):
            svc.format_session_summary("date", 1, 1, 0, 10.0, -1.0, 9.0)
        with pytest.raises(ValueError, match="Net PnL cannot be None"):
            svc.format_session_summary("date", 1, 1, 0, 10.0, 1.0, None)  # type: ignore

        with pytest.raises(ValueError, match="Status cannot be blank"):
            svc.format_graduation_status_message("", 10, 50.0, 100.0, 5.0, "blockers")

    def test_format_system_health_alert_message_validation(self) -> None:
        svc = TelegramService(enabled=False)
        valid = svc.format_system_health_alert_message(
            status="SYSTEM_DOWN",
            reason="Heartbeat silence > 120s",
            affected_components=["scraper", "database"],
            timestamp_str="2026-09-06 12:00:00 UTC",
        )
        assert "SYSTEM_DOWN" in valid
        assert "scraper" in valid

        with pytest.raises(ValueError, match="Affected components cannot be empty"):
            svc.format_system_health_alert_message("DOWN", "reason", [], "time")

    def test_format_daily_brief_and_session_summary_validation(self) -> None:
        svc = TelegramService(enabled=False)
        brief = svc.format_daily_brief(
            date_str="2026-09-06",
            market_overview="KSE-100 opening green",
            watchlist_summary=[{"ticker": "OGDC", "price": 142.0, "change_pct": 1.2}],
            key_levels=["140.0 Support"],
        )
        assert "OGDC" in brief
        assert "142.00" in brief

        summary = svc.format_session_summary(
            session_date="2026-09-06",
            trades_count=3,
            winning_trades=2,
            losing_trades=1,
            gross_pnl=5000.0,
            total_fees=350.0,
            net_pnl=4650.0,
            discipline_violations=0,
            ending_cash=504650.0,
        )
        assert "3" in summary
        assert "4,650.00" in summary
        assert "Clean" in summary


class TestHookPointsIntegration:
    def test_risk_engine_daily_halt_hook(self) -> None:
        engine = RiskEngine()
        sig = TradeSignal(
            signal_id="SIG_HALT_TEST",
            ticker="OGDC",
            entry_price=100.0,
            stop_loss=98.0,
            target_price=104.0,
            reward_risk_ratio=2.0,
            position_size=100,
            confidence_pct=70,
            invalidation_reason="Inv",
            created_at=datetime.now(timezone.utc),
            session_id="s",
        )

        with patch.object(telegram_service, "send_daily_halt_alert", return_value=True) as mock_halt:
            # Trigger daily loss breach: 15,000 loss on 500,000 capital = 3.0% > 2.0%
            assessment = engine.evaluate_signal(
                signal=sig,
                account_balance=500000.0,
                current_day_realized_loss=15000.0,
                trades_executed_today=0,
                current_time_pkt=time(11, 0),
                twenty_day_adv=5000000.0,
                open_positions=[],
            )
            assert assessment.is_approved is False
            assert any("daily_loss_limit" in r.rule_name for r in assessment.rule_results)
            mock_halt.assert_called_once()
            args, kwargs = mock_halt.call_args
            assert kwargs["loss_pct"] >= 2.0
            assert kwargs["loss_amount_pkr"] == 15000.0

    def test_paper_broker_level_hit_hook(self) -> None:
        ledger = DoubleEntryLedger(starting_balance_pkr=500000.0)
        broker = PaperBroker(ledger=ledger, persist_to_db=True)
        sig = TradeSignal(
            signal_id="SIG_EXIT_TEST",
            ticker="OGDC",
            entry_price=100.0,
            stop_loss=95.0,
            target_price=110.0,
            reward_risk_ratio=2.0,
            position_size=500,
            confidence_pct=70,
            invalidation_reason="Inv",
            created_at=datetime.now(timezone.utc),
            session_id="s",
        )
        with patch.object(broker, "_persist_trade_to_db"), patch.object(broker, "_persist_ledger_entries_to_db"):
            trade = broker.execute_buy(sig, scraped_price=100.0, shares=500)

            with patch.object(telegram_service, "send_level_hit_alert", return_value=True) as mock_exit:
                # Exit at target
                closed_trade = broker.execute_exit(trade.trade_id, scraped_price=110.0, exit_reason=ExitReason.TARGET_HIT)
                assert closed_trade.status == TradeStatus.CLOSED
                mock_exit.assert_called_once()
                args, kwargs = mock_exit.call_args
                assert kwargs["ticker"] == "OGDC"
                assert kwargs["level_type"] == "TARGET_HIT"
                assert kwargs["price"] == 110.0
                assert kwargs["net_pnl"] > 0

    def test_mistake_detector_hook(self) -> None:
        detector = MistakeDetector()
        bad_trade = DemoTrade(
            trade_id="TRD_MISTAKE_1",
            signal_id="SIG_1",
            ticker="OGDC",
            action=SignalAction.BUY,
            shares=100,
            entry_price=100.0,
            stop_loss=95.0,
            target_price=110.0,
            slippage_pct=0.002,
            filled_entry_price=100.2,
            opened_at=datetime.now(timezone.utc),
            entry_fees=50.0,
            fee_version="v1",
            session_id="s",
        )

        with patch.object(telegram_service, "send_mistake_alert", return_value=True) as mock_mistake:
            mistakes = detector.audit_trade(
                trade=bad_trade,
                account_balance_at_entry=500000.0,
                session_trades_count=4,  # Breaches max daily trades limit (3)
                entry_time_pkt=time(15, 5),  # Breaches 15:00 entry cutoff
            )
            assert len(mistakes) > 0
            assert any(m.rule_violated == "DAILY_TRADE_LIMIT_BREACH" for m in mistakes)
            assert any(m.rule_violated == "LATE_ENTRY_AFTER_CUTOFF" for m in mistakes)
            mock_mistake.assert_called()

        # Test discrepancy alert
        with patch.object(telegram_service, "send_mistake_alert", return_value=True) as mock_discrepancy:
            is_valid, msg = detector.verify_no_discrepancy(
                risk_engine_approved=True,
                audit_mistakes=mistakes,
            )
            assert is_valid is False
            mock_discrepancy.assert_called_once()

    def test_graduation_alert_hook(self) -> None:
        metrics = PerformanceMetrics(
            total_trades=32,
            winning_trades=20,
            losing_trades=12,
            win_rate_pct=62.5,
            total_net_pnl=45000.0,
            avg_win_pkr=3500.0,
            avg_loss_pkr=2000.0,
            profit_factor=2.91,
            expectancy_pkr=1437.5,
            max_drawdown_pct=3.2,
            recent_20_violations_count=0,
            is_graduated=True,
            graduation_blockers=[],
        )

        with patch.object(telegram_service, "send_graduation_alert", return_value=True) as mock_grad:
            sent = notify_graduation_status(metrics)
            assert sent is True
            mock_grad.assert_called_once()
            args, kwargs = mock_grad.call_args
            assert kwargs["status"] == "GRADUATED"
            assert kwargs["total_trades"] == 32

    def test_apscheduler_jobs_dispatch(self) -> None:
        with patch.object(telegram_service, "send_daily_brief", return_value=True) as mock_brief:
            ok_brief = run_daily_brief_job(date_str="2026-09-06")
            assert ok_brief is True
            mock_brief.assert_called_once()

        with patch.object(telegram_service, "send_session_summary", return_value=True) as mock_summary:
            ok_summary = run_session_summary_job(session_date="2026-09-06", trades_count=2, net_pnl=2500.0)
            assert ok_summary is True
            mock_summary.assert_called_once()

        # Verify scheduler creation
        sched = create_alert_scheduler(start=False)
        job_ids = [j.id for j in sched.get_jobs()]
        assert "telegram_daily_brief" in job_ids
        assert "telegram_session_summary" in job_ids

    def test_delivery_stats_query(self) -> None:
        stats = get_delivery_stats()
        assert isinstance(stats, dict)
        assert "total" in stats
        assert "sent" in stats
        assert "failed" in stats
        assert "pending" in stats
        assert "skipped" in stats

