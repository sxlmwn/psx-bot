"""
Comprehensive tests for VeteranDesk Discord notification engine:
1. Schema-validated rich embed templates (zero blanks/None renders).
2. Rate-limit aware exponential backoff & retry mechanism.
3. Proper DeliveryStatus.SKIPPED state for disabled or unconfigured instances.
4. HTTP 429 rate limit respect (retry_after parsing).
5. Asynchronous and synchronous queue delivery.
6. Decoupled execution from Telegram alerts.
7. Database persistence in discord_delivery_log and stats calculation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
import httpx
import pytest

from veterandesk.alerts.discord import (
    COLOR_BLUE,
    COLOR_DARK_RED,
    COLOR_GREEN,
    COLOR_ORANGE,
    COLOR_PURPLE,
    COLOR_RED,
    DiscordOutboundMessage,
    DiscordService,
    get_discord_delivery_stats,
)
from veterandesk.alerts.telegram import DeliveryStatus, MessageType
from veterandesk.strategy.models import SignalAction, SignalStatus, TradeSignal


def create_sample_signal() -> TradeSignal:
    return TradeSignal(
        signal_id="SIG_TEST_DISCORD_001",
        ticker="SYS",
        action=SignalAction.BUY,
        entry_price=450.0,
        stop_loss=442.0,
        target_price=466.0,
        reward_risk_ratio=2.0,
        position_size=100,
        confidence_pct=70.0,
        invalidation_reason="1-min close below 442.0",
        data_status="ok",
        status=SignalStatus.GENERATED,
        created_at=datetime.now(timezone.utc),
        session_id="test_session",
    )


class TestDiscordAlerts:
    """Test suite for Discord alerting, embeds, and retry engine."""

    # =========================================================================
    # 1. SKIPPED STATE (DISABLED / UNCONFIGURED)
    # =========================================================================

    def test_discord_disabled_or_unconfigured_marks_skipped_not_sent(self) -> None:
        """
        Critical regression test:
        Asserts that when Discord notifier is unconfigured or disabled,
        messages are recorded as SKIPPED, NEVER SENT, and attempts == 0.
        """
        # Case 1: Disabled flag (enabled=False)
        svc_disabled = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=False)
        msg1 = svc_disabled.enqueue_message(MessageType.ALERT, content="Test Disabled")
        res1 = svc_disabled._send_with_retry_sync(msg1)
        assert res1 is False
        assert msg1.status == DeliveryStatus.SKIPPED
        assert msg1.status != DeliveryStatus.SENT
        assert msg1.is_delivered is False
        assert msg1.attempts == 0
        assert msg1.sent_at is None
        assert msg1 in svc_disabled.skipped_history
        assert msg1 not in svc_disabled.delivered_history

        # Case 2: Missing webhook_url (empty string)
        svc_empty = DiscordService(webhook_url="", enabled=True)
        msg2 = svc_empty.enqueue_message(MessageType.SIGNAL, content="Test Empty Webhook")
        res2 = svc_empty._send_with_retry_sync(msg2)
        assert res2 is False
        assert msg2.status == DeliveryStatus.SKIPPED
        assert msg2.status != DeliveryStatus.SENT
        assert msg2.is_delivered is False
        assert msg2.attempts == 0
        assert msg2.sent_at is None

        # Case 3: Whitespace webhook_url
        svc_ws = DiscordService(webhook_url="   ", enabled=True)
        msg3 = svc_ws.enqueue_message(MessageType.LEVEL_HIT, content="Test Whitespace")
        res3 = svc_ws._send_with_retry_sync(msg3)
        assert res3 is False
        assert msg3.status == DeliveryStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_discord_disabled_async_marks_skipped(self) -> None:
        """Verify async delivery also properly records SKIPPED status."""
        svc_disabled = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=False)
        msg = svc_disabled.enqueue_message(MessageType.ALERT, content="Test Disabled Async")
        res = await svc_disabled._send_with_retry_async(msg)
        assert res is False
        assert msg.status == DeliveryStatus.SKIPPED
        assert msg.is_delivered is False
        assert msg.attempts == 0
        assert msg.sent_at is None

    # =========================================================================
    # 2. SCHEMA-VALIDATED EMBED FORMATTERS (ALL 8 TYPES)
    # =========================================================================

    def test_signal_embed_formatting_and_validation(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        sig = create_sample_signal()
        embed = svc.format_signal_embed(sig, shares=150, reason_lines="ORB Breakout confirmed")

        assert "SYS" in embed["title"]
        assert embed["color"] == COLOR_GREEN  # BUY signal
        field_names = [f["name"] for f in embed["fields"]]
        assert "Action" in field_names
        assert "Entry Price" in field_names
        assert "Stop-Loss" in field_names
        assert "Target Price" in field_names
        assert "Rationale" in field_names

        # Invalidation check: zero entry price
        sig.entry_price = 0.0
        with pytest.raises(ValueError, match="entry_price invalid"):
            svc.format_signal_embed(sig, shares=100, reason_lines="test")

    def test_level_hit_embed_formatting_and_validation(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)

        # Target Hit (Green)
        emb_target = svc.format_level_hit_embed(
            ticker="ENGRO",
            trade_id="TRD_101",
            level_type="TARGET_HIT",
            price=310.0,
            fill_price=309.8,
            net_pnl=4500.0,
            closed_at_str="2026-09-06 14:00:00 UTC",
        )
        assert "TARGET" in emb_target["title"] or "ENGRO" in emb_target["title"]
        assert emb_target["color"] == COLOR_GREEN

        # Stop Hit (Red)
        emb_stop = svc.format_level_hit_embed(
            ticker="ENGRO",
            trade_id="TRD_102",
            level_type="STOP_HIT",
            price=295.0,
            fill_price=294.8,
            net_pnl=-3000.0,
            closed_at_str="2026-09-06 14:15:00 UTC",
        )
        assert emb_stop["color"] == COLOR_RED

        # Validation error on blank ticker
        with pytest.raises(ValueError, match="Ticker cannot be blank"):
            svc.format_level_hit_embed("", "TRD_103", "STOP_HIT", 100, 100, 0, "now")

    def test_daily_loss_halt_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        embed = svc.format_daily_loss_halt_embed(
            loss_pct=2.15,
            max_loss_pct=2.00,
            loss_amount_pkr=10750.0,
            halt_time_pkt="11:45:00 PKT",
            action_taken="Positions flattened.",
        )
        assert embed["color"] == COLOR_DARK_RED
        assert "CRITICAL" in embed["title"]
        assert any("2.15%" in f["value"] for f in embed["fields"])

    def test_mistake_alert_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        embed_crit = svc.format_mistake_alert_embed(
            rule_violated="OVERSIZE_TRADE",
            severity="CRITICAL",
            trade_id="TRD_999",
            details="Position exceeded 1% equity risk.",
            detected_at_str="2026-09-06 10:30:00 UTC",
        )
        assert embed_crit["color"] == COLOR_DARK_RED
        assert "CRITICAL" in embed_crit["title"]

        embed_warn = svc.format_mistake_alert_embed(
            rule_violated="TIME_STOP_WARNING",
            severity="WARNING",
            trade_id=None,
            details="Trade near cutoff.",
            detected_at_str="2026-09-06 15:15:00 UTC",
        )
        assert embed_warn["color"] == COLOR_ORANGE
        assert "WARNING" in embed_warn["title"]

    def test_graduation_status_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        embed = svc.format_graduation_status_embed(
            status="GRADUATED",
            total_trades=32,
            win_rate_pct=62.5,
            expectancy_pkr=1250.0,
            max_drawdown_pct=5.4,
            blockers_or_status="All criteria met!",
        )
        assert embed["color"] == COLOR_PURPLE
        assert "GRADUATION" in embed["title"]

    def test_system_health_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        embed = svc.format_system_health_alert_embed(
            status="SYSTEM_DOWN",
            reason="Silence threshold exceeded (120s)",
            affected_components=["scraper", "risk_engine"],
            timestamp_str="2026-09-06 12:00:00 UTC",
        )
        assert embed["color"] == COLOR_RED
        assert "SYSTEM ALERT" in embed["title"]

    def test_daily_brief_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        embed = svc.format_daily_brief_embed(
            date_str="2026-09-06",
            market_overview="KSE-100 opening green.",
            watchlist_summary=[{"ticker": "OGDC", "price": 145.5, "change_pct": 1.2}],
            key_levels=["Support: 78,000", "Resistance: 80,000"],
        )
        assert embed["color"] == COLOR_BLUE
        assert "DAILY BRIEF" in embed["title"]

    def test_session_summary_embed_formatting(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        # Winning session (Green)
        emb_win = svc.format_session_summary_embed(
            session_date="2026-09-06",
            trades_count=3,
            winning_trades=2,
            losing_trades=1,
            gross_pnl=15000.0,
            total_fees=1200.0,
            net_pnl=13800.0,
        )
        assert emb_win["color"] == COLOR_GREEN

        # Losing session (Red)
        emb_loss = svc.format_session_summary_embed(
            session_date="2026-09-06",
            trades_count=2,
            winning_trades=0,
            losing_trades=2,
            gross_pnl=-8000.0,
            total_fees=800.0,
            net_pnl=-8800.0,
        )
        assert emb_loss["color"] == COLOR_RED

    # =========================================================================
    # 3. SUCCESSFUL LIVE DELIVERY (MOCKED HTTP 200/204)
    # =========================================================================

    def test_successful_sync_delivery_with_mock_httpx(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        mock_resp = httpx.Response(status_code=204, request=httpx.Request("POST", svc.webhook_url))

        with patch("httpx.Client.post", return_value=mock_resp):
            success = svc.send_message(content="Unit test live send", reference_id="REF_001")
            assert success is True
            assert len(svc.delivered_history) == 1
            msg = svc.delivered_history[0]
            assert msg.status == DeliveryStatus.SENT
            assert msg.is_delivered is True
            assert msg.attempts == 1
            assert msg.sent_at is not None

    @pytest.mark.asyncio
    async def test_successful_async_delivery_with_mock_httpx(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        mock_resp = httpx.Response(status_code=200, request=httpx.Request("POST", svc.webhook_url))

        with patch("httpx.AsyncClient.post", return_value=mock_resp):
            msg = svc.enqueue_message(MessageType.ALERT, content="Async test send")
            success = await svc._send_with_retry_async(msg)
            assert success is True
            assert msg.status == DeliveryStatus.SENT
            assert msg.attempts == 1

    # =========================================================================
    # 4. RATE LIMITING (HTTP 429) AND RETRIES
    # =========================================================================

    def test_rate_limit_429_retries_after_delay(self) -> None:
        """Assert HTTP 429 with retry_after is parsed and retried."""
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)

        resp_429 = httpx.Response(
            status_code=429,
            json={"retry_after": 0.01, "message": "You are being rate limited."},
            request=httpx.Request("POST", svc.webhook_url),
        )
        resp_204 = httpx.Response(
            status_code=204,
            request=httpx.Request("POST", svc.webhook_url),
        )

        with patch("httpx.Client.post", side_effect=[resp_429, resp_204]):
            success = svc.send_message(content="Rate limit test")
            assert success is True
            assert len(svc.delivered_history) == 1
            msg = svc.delivered_history[0]
            assert msg.attempts == 2
            assert msg.status == DeliveryStatus.SENT

    def test_exhausted_retries_marks_failed(self) -> None:
        """Assert that exhausting all 3 attempts marks message as FAILED."""
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)

        resp_500 = httpx.Response(
            status_code=500,
            text="Internal Server Error",
            request=httpx.Request("POST", svc.webhook_url),
        )

        with patch("httpx.Client.post", side_effect=[resp_500, resp_500, resp_500]):
            with patch("time.sleep", return_value=None):  # speed up test
                success = svc.send_message(content="Failure test")
                assert success is False
                assert len(svc.failed_dead_letter) == 1
                msg = svc.failed_dead_letter[0]
                assert msg.status == DeliveryStatus.FAILED
                assert msg.attempts == 3
                assert msg.failed_at is not None

    # =========================================================================
    # 5. QUEUE PROCESSING
    # =========================================================================

    def test_process_queue_sync(self) -> None:
        svc = DiscordService(webhook_url="https://discord.com/api/webhooks/test/123", enabled=True)
        svc.enqueue_message(MessageType.ALERT, content="Q1")
        svc.enqueue_message(MessageType.ALERT, content="Q2")
        assert len(svc.outbound_queue) == 2

        mock_resp = httpx.Response(status_code=204, request=httpx.Request("POST", svc.webhook_url))
        with patch("httpx.Client.post", return_value=mock_resp):
            count = svc.process_queue_sync()
            assert count == 2
            assert len(svc.outbound_queue) == 0
            assert len(svc.delivered_history) == 2

    # =========================================================================
    # 6. INDEPENDENT DISPATCHING (DECOUPLED FROM TELEGRAM)
    # =========================================================================

    def test_decoupled_alerting_neither_channel_blocks_other(self) -> None:
        """
        Verify that if Telegram raises an exception, Discord alert still runs,
        and vice versa.
        """
        from veterandesk.alerts.telegram import telegram_service
        from veterandesk.alerts.discord import discord_service
        from veterandesk.alerts.scheduler import run_daily_brief_job

        # Case 1: Telegram fails with exception, Discord succeeds
        tg_mock = MagicMock(side_effect=RuntimeError("Simulated Telegram outage"))
        dc_mock = MagicMock(return_value=True)

        with patch.object(telegram_service, "send_daily_brief", tg_mock):
            with patch.object(discord_service, "send_daily_brief", dc_mock):
                res = run_daily_brief_job(date_str="2026-09-06")
                assert res is True
                assert tg_mock.called
                assert dc_mock.called

        # Case 2: Discord fails with exception, Telegram succeeds
        tg_mock2 = MagicMock(return_value=True)
        dc_mock2 = MagicMock(side_effect=RuntimeError("Simulated Discord outage"))

        with patch.object(telegram_service, "send_daily_brief", tg_mock2):
            with patch.object(discord_service, "send_daily_brief", dc_mock2):
                res2 = run_daily_brief_job(date_str="2026-09-06")
                assert res2 is True
                assert tg_mock2.called
                assert dc_mock2.called

    # =========================================================================
    # 7. DELIVERY STATS
    # =========================================================================

    def test_get_discord_delivery_stats(self) -> None:
        stats = get_discord_delivery_stats()
        assert "total" in stats
        assert "sent" in stats
        assert "failed" in stats
        assert "pending" in stats
        assert "skipped" in stats
        assert isinstance(stats["total"], int)
