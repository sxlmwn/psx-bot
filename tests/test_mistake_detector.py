"""
Tests for Independent Mistake Detection & Audit Module.
Requirement: Catches any rule bypass, discrepancy, or risk failure.
"""

from datetime import datetime, time, timezone
import pytest

from veterandesk.audit.mistake_detector import MistakeDetector, MistakeSeverity
from veterandesk.execution.paper_broker import DemoTrade, ExitReason
from veterandesk.strategy.models import SignalAction


class TestMistakeDetector:
    def test_detects_no_stop_loss(self):
        detector = MistakeDetector()
        # Direct object creation to test audit resilience
        trade = DemoTrade.__new__(DemoTrade)
        trade.trade_id = "TRD_BAD"
        trade.action = SignalAction.BUY
        trade.shares = 100
        trade.filled_entry_price = 100.0
        trade.stop_loss = 0.0  # Missing stop
        trade.exit_price = None

        mistakes = detector.audit_trade(
            trade=trade,
            account_balance_at_entry=500000.0,
            session_trades_count=1,
            entry_time_pkt=time(10, 0, 0),
        )
        assert any(m.rule_violated == "NO_STOP_LOSS" for m in mistakes)

    def test_detects_oversize_risk(self):
        detector = MistakeDetector(max_risk_pct=1.00)
        trade = DemoTrade(
            trade_id="TRD_OVERSIZE",
            signal_id="SIG_1",
            ticker="OGDC",
            action=SignalAction.BUY,
            shares=2000,
            entry_price=100.0,
            stop_loss=95.0,
            target_price=110.0,
            slippage_pct=0.002,
            filled_entry_price=100.20
        )
        # Account balance 500,000 -> 1% max risk is PKR 5,000
        # Risk = 2000 * (100.20 - 95.00) = 10,400 (2.08% > 1.00%)
        mistakes = detector.audit_trade(
            trade=trade,
            account_balance_at_entry=500000.0,
            session_trades_count=1,
            entry_time_pkt=time(10, 0, 0),
        )
        assert any(m.rule_violated == "OVERSIZE_RISK_LIMIT_EXCEEDED" for m in mistakes)

    def test_detects_late_entry_after_cutoff(self):
        detector = MistakeDetector(entry_cutoff_pkt=time(15, 0, 0))
        trade = DemoTrade(
            trade_id="TRD_LATE",
            signal_id="SIG_1",
            ticker="OGDC",
            action=SignalAction.BUY,
            shares=100,
            entry_price=100.0,
            stop_loss=95.0,
            target_price=110.0,
            slippage_pct=0.002,
            filled_entry_price=100.20
        )
        # Entry at 15:05 PKT -> Cutoff was 15:00
        mistakes = detector.audit_trade(
            trade=trade,
            account_balance_at_entry=500000.0,
            session_trades_count=1,
            entry_time_pkt=time(15, 5, 0),
        )
        assert any(m.rule_violated == "LATE_ENTRY_AFTER_CUTOFF" for m in mistakes)

    def test_discrepancy_alert_when_risk_engine_bypassed(self):
        detector = MistakeDetector()
        # Simulated scenario: Risk Engine marked trade as approved, but audit found 2 violations
        from veterandesk.audit.mistake_detector import DetectedMistake
        simulated_mistakes = [
            DetectedMistake(
                id="M1",
                trade_id="TRD_X",
                rule_violated="OVERSIZE_RISK_LIMIT_EXCEEDED",
                severity=MistakeSeverity.CRITICAL,
                details="Oversize",
                detected_at=datetime.now(timezone.utc)
            )
        ]
        is_ok, alert = detector.verify_no_discrepancy(
            risk_engine_approved=True,
            audit_mistakes=simulated_mistakes
        )
        assert is_ok is False
        assert alert is not None
        assert "DISCREPANCY ALERT" in alert
