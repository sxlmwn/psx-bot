"""
Tests for Risk & Discipline Engine.
Target: 100% coverage including all boundary conditions:
- Exactly at 1.00% risk limit
- Above 1.00% risk limit
- Exactly 2.00% daily loss limit
- Daily loss halt persistence
- Exactly at 15:00:00 PKT entry cutoff
- After 15:00:00 PKT entry cutoff
- Exactly 3 intraday trades
- 4th trade rejected
- Zero/negative balance, zero volume, stop >= entry
- Liquidity cap (> 5% ADV)
- Sizing formula with lot rounding
"""

import pytest
from datetime import time
from veterandesk.risk.rules import (
    check_per_trade_risk,
    check_daily_loss_limit,
    check_max_intraday_trades,
    check_entry_time_cutoff,
    check_force_close_time,
    check_liquidity_cap,
    check_averaging_down,
    calculate_position_size,
)
from veterandesk.risk.engine import RiskEngine
from veterandesk.strategy.models import TradeSignal, SignalAction


class TestAtomicRiskRules:
    def test_per_trade_risk_boundaries(self):
        # 1. Exactly 1% risk on PKR 500,000 balance -> max risk PKR 5,000
        # Entry 100, Stop 95 -> risk/share = 5 -> 1000 shares = 5000 risk (exactly 1%)
        res = check_per_trade_risk(
            account_balance=500000.0,
            entry_price=100.0,
            stop_loss=95.0,
            shares=1000,
            max_risk_pct=1.00
        )
        assert res.passed is True

        # 2. Over 1% risk: 1001 shares -> risk 5005 (1.001%)
        res_over = check_per_trade_risk(
            account_balance=500000.0,
            entry_price=100.0,
            stop_loss=95.0,
            shares=1001,
            max_risk_pct=1.00
        )
        assert res_over.passed is False
        assert "exceeds maximum allowed" in res_over.reason

        # 3. Invalid account balance
        assert check_per_trade_risk(0.0, 100.0, 95.0, 100).passed is False
        assert check_per_trade_risk(-500.0, 100.0, 95.0, 100).passed is False

        # 4. Stop >= Entry
        assert check_per_trade_risk(500000.0, 100.0, 100.0, 100).passed is False
        assert check_per_trade_risk(500000.0, 100.0, 105.0, 100).passed is False

        # 5. Invalid prices or shares
        assert check_per_trade_risk(500000.0, -10.0, 5.0, 100).passed is False
        assert check_per_trade_risk(500000.0, 100.0, -5.0, 100).passed is False
        assert check_per_trade_risk(500000.0, 100.0, 90.0, 0).passed is False

    def test_daily_loss_limit_boundaries(self):
        # Balance PKR 500,000, max daily loss 2% = PKR 10,000
        # 1. Exactly 2% loss -> Halt
        res = check_daily_loss_limit(
            current_day_realized_loss=10000.0,
            account_balance=500000.0,
            max_daily_loss_pct=2.00
        )
        assert res.passed is False
        assert "reached/exceeded daily limit" in res.reason

        # 2. Under limit: PKR 9,999 -> Pass
        res_under = check_daily_loss_limit(
            current_day_realized_loss=9999.0,
            account_balance=500000.0,
            max_daily_loss_pct=2.00
        )
        assert res_under.passed is True

        # 3. Already halted state
        res_halted = check_daily_loss_limit(
            current_day_realized_loss=0.0,
            account_balance=500000.0,
            is_already_halted=True
        )
        assert res_halted.passed is False
        assert "halted for the day" in res_halted.reason

        # 4. Invalid balance
        assert check_daily_loss_limit(100.0, 0.0).passed is False

    def test_max_intraday_trades(self):
        # 0, 1, 2 trades -> PASS
        assert check_max_intraday_trades(0, max_trades=3).passed is True
        assert check_max_intraday_trades(1, max_trades=3).passed is True
        assert check_max_intraday_trades(2, max_trades=3).passed is True

        # Exactly 3 trades -> FAIL
        res_limit = check_max_intraday_trades(3, max_trades=3)
        assert res_limit.passed is False
        assert "Daily trade limit reached" in res_limit.reason

        # 4 trades -> FAIL
        assert check_max_intraday_trades(4, max_trades=3).passed is False

    def test_entry_time_cutoff(self):
        # Before 15:00:00 -> PASS
        assert check_entry_time_cutoff(time(14, 59, 59), time(15, 0, 0)).passed is True
        assert check_entry_time_cutoff(time(9, 30, 0), time(15, 0, 0)).passed is True

        # Exactly 15:00:00 -> FAIL
        res_exact = check_entry_time_cutoff(time(15, 0, 0), time(15, 0, 0))
        assert res_exact.passed is False
        assert "at or after entry cutoff" in res_exact.reason

        # After 15:00:00 -> FAIL
        assert check_entry_time_cutoff(time(15, 1, 0), time(15, 0, 0)).passed is False

    def test_force_close_time(self):
        # Before 15:20 -> PASS
        assert check_force_close_time(time(15, 19, 59), time(15, 20, 0)).passed is True

        # At or after 15:20 -> FAIL
        res_exact = check_force_close_time(time(15, 20, 0), time(15, 20, 0))
        assert res_exact.passed is False
        assert "reached force close threshold" in res_exact.reason

    def test_liquidity_cap(self):
        # 20-day ADV = 100,000 shares. 5% cap = 5,000 shares.
        assert check_liquidity_cap(5000, 100000.0, max_adv_pct=5.0).passed is True
        assert check_liquidity_cap(5001, 100000.0, max_adv_pct=5.0).passed is False
        assert check_liquidity_cap(100, 0.0).passed is False

    def test_anti_averaging_down(self):
        open_positions = [{"ticker": "OGDC", "shares": 500}]
        # Adding to existing position without pre-plan -> FAIL
        assert check_averaging_down("OGDC", open_positions, is_pre_planned=False).passed is False
        # Pre-planned add -> PASS
        assert check_averaging_down("OGDC", open_positions, is_pre_planned=True).passed is True
        # New ticker -> PASS
        assert check_averaging_down("PPL", open_positions, is_pre_planned=False).passed is True

    def test_position_sizing_formula(self):
        # Balance = 500,000, Risk = 1% = 5,000 PKR
        # Entry = 100, Stop = 95 -> Diff = 5 -> floor(5000 / 5) = 1000 shares
        assert calculate_position_size(500000.0, 100.0, 95.0, risk_pct=1.00, lot_size=1) == 1000

        # Lot size = 100: 1050 shares rounds down to 1000
        # Entry 100, Stop 95.2 -> Diff = 4.8 -> floor(5000 / 4.8) = 1041 -> rounded down to 1000
        assert calculate_position_size(500000.0, 100.0, 95.2, risk_pct=1.00, lot_size=100) == 1000

        # Invalid cases return 0
        assert calculate_position_size(0, 100.0, 95.0) == 0
        assert calculate_position_size(500000.0, 95.0, 100.0) == 0  # Entry <= Stop
        assert calculate_position_size(500000.0, 100.0, 95.0, risk_pct=0) == 0


class TestRiskEnginePipeline:
    def test_pipeline_approval_and_rejection(self):
        engine = RiskEngine(
            max_risk_per_trade_pct=1.00,
            max_daily_loss_pct=2.00,
            max_intraday_trades=3,
            entry_cutoff_pkt=time(15, 0, 0),
            force_close_pkt=time(15, 20, 0),
            max_adv_pct=5.00,
            lot_size=1
        )

        from datetime import datetime, timezone
        sig = TradeSignal(
            signal_id="TEST_SIG_1",
            ticker="OGDC",
            entry_price=100.0,
            stop_loss=95.0,
            target_price=107.5,
            reward_risk_ratio=1.5,
            position_size=0,
            confidence_pct=60,
            invalidation_reason="Test invalidation",
            created_at=datetime.now(timezone.utc),
            session_id="test_sess"
        )

        # 1. Healthy conditions -> Approved
        assessment = engine.evaluate_signal(
            signal=sig,
            account_balance=500000.0,
            current_day_realized_loss=0.0,
            trades_executed_today=1,
            current_time_pkt=time(10, 30, 0),
            twenty_day_adv=100000.0,
            open_positions=[],
            is_already_halted=False
        )
        assert assessment.is_approved is True
        assert assessment.approved_shares == 1000
        assert len(assessment.rejection_reasons) == 0
        assert assessment.risk_pct_used == 1.00

        # 2. Daily Loss limit reached -> Rejected
        assessment_loss = engine.evaluate_signal(
            signal=sig,
            account_balance=500000.0,
            current_day_realized_loss=10500.0,
            trades_executed_today=1,
            current_time_pkt=time(10, 30, 0),
            twenty_day_adv=100000.0,
            open_positions=[],
            is_already_halted=False
        )
        assert assessment_loss.is_approved is False
        assert any("daily limit" in r for r in assessment_loss.rejection_reasons)

        # 3. Time cutoff passed -> Rejected
        assessment_time = engine.evaluate_signal(
            signal=sig,
            account_balance=500000.0,
            current_day_realized_loss=0.0,
            trades_executed_today=1,
            current_time_pkt=time(15, 5, 0),
            twenty_day_adv=100000.0,
            open_positions=[],
            is_already_halted=False
        )
        assert assessment_time.is_approved is False
        assert any("entry cutoff" in r for r in assessment_time.rejection_reasons)

        # 4. Zero balance -> Sizing 0 shares -> Rejected
        assessment_zero = engine.evaluate_signal(
            signal=sig,
            account_balance=0.0,
            current_day_realized_loss=0.0,
            trades_executed_today=1,
            current_time_pkt=time(10, 30, 0),
            twenty_day_adv=100000.0,
            open_positions=[],
            is_already_halted=False
        )
        assert assessment_zero.is_approved is False
        assert any("Position size computed to 0" in r for r in assessment_zero.rejection_reasons)

    def test_constructor_ceiling_validation(self):
        with pytest.raises(ValueError, match="max_risk_per_trade_pct cannot exceed 1.00%"):
            RiskEngine(max_risk_per_trade_pct=1.05)

        with pytest.raises(ValueError, match="max_daily_loss_pct cannot exceed 5.00%"):
            RiskEngine(max_daily_loss_pct=6.0)
