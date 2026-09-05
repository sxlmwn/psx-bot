"""
Tests for System Health Monitor and Telegram Service.
"""

from datetime import datetime, timezone
import pytest

from veterandesk.alerts.telegram import MessageType, TelegramService
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.health.monitor import ComponentStatus, SystemHealthMonitor
from veterandesk.strategy.models import SignalAction, TradeSignal


class TestHealthAndAlerts:
    def test_health_monitor_heartbeat_and_down_detection(self):
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

    @pytest.mark.asyncio
    async def test_telegram_message_formatting_and_queue(self):
        svc = TelegramService(bot_token="fake_token", chat_id="fake_chat", enabled=False)

        sig = TradeSignal(
            signal_id="SIG_TEL_1",
            ticker="OGDC",
            entry_price=142.5,
            stop_loss=139.8,
            target_price=146.55,
            reward_risk_ratio=1.5,
            position_size=1000,
            confidence_pct=65,
            invalidation_reason="Close below range high",
            created_at=datetime.now(timezone.utc),
            session_id="sess_t"
        )

        text = svc.format_signal_message(
            signal=sig,
            shares=1000,
            reason_lines="ORB Breakout on 2.1x volume surge.\nRisk capped at 0.95%."
        )
        assert "VETERANDESK TRADE SIGNAL" in text
        assert "OGDC" in text
        assert "142.50" in text

        # Queue message
        msg = svc.enqueue_message(MessageType.SIGNAL, text)
        assert len(svc.outbound_queue) == 1

        # Process queue (in mock/disabled mode)
        delivered = await svc.process_queue()
        assert delivered == 1
        assert len(svc.outbound_queue) == 0
        assert len(svc.delivered_history) == 1
        assert svc.delivered_history[0].is_delivered is True
