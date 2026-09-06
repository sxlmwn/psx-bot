"""
Discord Alerts and Webhook Notification Service.

Features:
1. Outbound delivery with exponential backoff and retry (up to 3 attempts).
2. Rate-limit aware handling (respects Discord HTTP 429 retry_after).
3. Schema-validated rich embeds for all 8 notification types.
4. Persistent DB delivery tracking in `discord_delivery_log` (pending -> sent / failed / skipped).
5. Async and synchronous delivery support.
6. Graceful offline/disabled mode when webhook URL is unconfigured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Dict, List, Optional
import uuid
import httpx
from sqlalchemy import text

from veterandesk.alerts.telegram import DeliveryStatus, MessageType
from veterandesk.alerts.validators import (
    validate_daily_brief,
    validate_daily_loss_halt,
    validate_graduation_status,
    validate_level_hit,
    validate_mistake_alert,
    validate_session_summary,
    validate_signal,
    validate_system_health_alert,
)
from veterandesk.config import settings
from veterandesk.logging import get_logger
from veterandesk.strategy.models import TradeSignal

logger = get_logger("veterandesk.discord")


# Discord embed colors (hex integers)
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_DARK_RED = 0x992D22
COLOR_ORANGE = 0xE67E22
COLOR_PURPLE = 0x9B59B6
COLOR_BLUE = 0x3498DB


@dataclass
class DiscordOutboundMessage:
    id: str
    msg_type: MessageType
    content: Optional[str] = None
    embeds: List[Dict[str, Any]] = field(default_factory=list)
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    is_delivered: bool = False
    reference_id: Optional[str] = None
    event_type: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None

    @property
    def delivered_at(self) -> Optional[datetime]:
        return self.sent_at

    @delivered_at.setter
    def delivered_at(self, dt: Optional[datetime]) -> None:
        self.sent_at = dt

    @property
    def payload_dict(self) -> Dict[str, Any]:
        p: Dict[str, Any] = {}
        if self.content:
            p["content"] = self.content
        if self.embeds:
            p["embeds"] = self.embeds
        return p

    @property
    def payload_json(self) -> str:
        return json.dumps(self.payload_dict)


class DiscordService:
    """
    Robust Discord notification service using incoming webhooks, rich embeds,
    exponential retry backoff, HTTP 429 rate-limit awareness, and DB persistence.
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.webhook_url: Optional[str] = (
            webhook_url
            if webhook_url is not None
            else (settings.discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL"))
        )

        env_enabled = settings.discord_enabled
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = bool(env_enabled and self.webhook_url and self.webhook_url.strip())

        self.outbound_queue: List[DiscordOutboundMessage] = []
        self.delivered_history: List[DiscordOutboundMessage] = []
        self.failed_dead_letter: List[DiscordOutboundMessage] = []
        self.skipped_history: List[DiscordOutboundMessage] = []
        self.last_send_timestamp: float = 0.0

    # =========================================================================
    # EMBED BUILDERS & TEMPLATES (SCHEMA-VALIDATED)
    # =========================================================================

    def format_signal_embed(self, signal: TradeSignal, shares: int, reason_lines: str) -> Dict[str, Any]:
        """Format and validate trade signal Discord embed."""
        validate_signal(signal, shares, reason_lines)

        is_buy = str(signal.action.value).upper() == "BUY"
        color = COLOR_GREEN if is_buy else COLOR_RED

        fields = [
            {"name": "Action", "value": f"**{signal.action.value}**", "inline": True},
            {"name": "Ticker", "value": f"`{signal.ticker.upper()}`", "inline": True},
            {"name": "Quantity", "value": f"{shares:,} shares", "inline": True},
            {"name": "Entry Price", "value": f"PKR {signal.entry_price:.2f}", "inline": True},
            {"name": "Stop-Loss", "value": f"PKR {signal.stop_loss:.2f}", "inline": True},
            {"name": "Target Price", "value": f"PKR {signal.target_price:.2f}", "inline": True},
            {"name": "Reward / Risk", "value": f"{signal.reward_risk_ratio:.2f}:1", "inline": True},
            {"name": "Confidence", "value": f"{signal.confidence_pct}%", "inline": True},
            {"name": "Strategy", "value": f"`{signal.strategy}`", "inline": True},
            {"name": "Rationale", "value": reason_lines.strip(), "inline": False},
        ]

        embed = {
            "title": f"🎯 TRADE SIGNAL — {signal.ticker.upper()}",
            "color": color,
            "fields": fields,
            "timestamp": signal.created_at.isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Signal Engine"},
        }
        return embed

    def format_level_hit_embed(
        self,
        ticker: str,
        trade_id: str,
        level_type: str,
        price: float,
        fill_price: float,
        net_pnl: float,
        closed_at_str: str,
    ) -> Dict[str, Any]:
        """Format level hit alert embed (Target, Stop, or Time Cutoff)."""
        validate_level_hit(ticker, trade_id, level_type, price, fill_price, net_pnl, closed_at_str)

        is_target = "TARGET" in level_type.upper()
        is_stop = "STOP" in level_type.upper()
        color = COLOR_GREEN if is_target else (COLOR_RED if is_stop else COLOR_ORANGE)
        emoji = "🎯" if is_target else ("🛑" if is_stop else "⏰")
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
        sign = "+" if net_pnl >= 0 else ""

        fields = [
            {"name": "Event", "value": f"`{level_type}`", "inline": True},
            {"name": "Trade ID", "value": f"`{trade_id}`", "inline": True},
            {"name": "Ticker", "value": f"`{ticker.upper()}`", "inline": True},
            {"name": "Trigger Price", "value": f"PKR {price:.2f}", "inline": True},
            {"name": "Fill Price", "value": f"PKR {fill_price:.2f}", "inline": True},
            {"name": "Realized Net PnL", "value": f"{pnl_emoji} PKR {sign}{net_pnl:>+10,.2f}".strip(), "inline": True},
            {"name": "Closed At", "value": closed_at_str, "inline": False},
        ]

        embed = {
            "title": f"{emoji} LEVEL HIT — {ticker.upper()}",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Trade Lifecycle"},
        }
        return embed

    def format_daily_loss_halt_embed(
        self,
        loss_pct: float,
        max_loss_pct: float,
        loss_amount_pkr: float,
        halt_time_pkt: str,
        action_taken: str,
    ) -> Dict[str, Any]:
        """Format daily loss halt critical notification embed."""
        validate_daily_loss_halt(loss_pct, max_loss_pct, loss_amount_pkr, halt_time_pkt, action_taken)

        fields = [
            {"name": "Daily Loss Reached", "value": f"**{loss_pct:.2f}%** (Limit: {max_loss_pct:.2f}%)", "inline": True},
            {"name": "Total Realized Loss", "value": f"PKR {loss_amount_pkr:>+10,.2f}", "inline": True},
            {"name": "Trigger Time", "value": halt_time_pkt, "inline": True},
            {"name": "Enforcement Action", "value": action_taken.strip(), "inline": False},
            {"name": "Discipline Invariant", "value": "🔒 All trading is blocked until next market session.", "inline": False},
        ]

        embed = {
            "title": "🚨 CRITICAL DISCIPLINE HALT TRIGGERED",
            "color": COLOR_DARK_RED,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Risk Engine"},
        }
        return embed

    def format_mistake_alert_embed(
        self,
        rule_violated: str,
        severity: str,
        trade_id: Optional[str],
        details: str,
        detected_at_str: str,
    ) -> Dict[str, Any]:
        """Format independent mistake detection audit alert embed."""
        validate_mistake_alert(rule_violated, severity, details, detected_at_str)

        is_crit = severity.upper() == "CRITICAL"
        color = COLOR_DARK_RED if is_crit else COLOR_ORANGE
        badge = "🚨 CRITICAL" if is_crit else "⚠️ WARNING"
        trd_ref = f"`{trade_id.strip()}`" if trade_id and trade_id.strip() else "`N/A (System-wide)`"

        fields = [
            {"name": "Rule Violated", "value": f"`{rule_violated}`", "inline": True},
            {"name": "Severity", "value": f"`{severity.upper()}`", "inline": True},
            {"name": "Trade Ref", "value": trd_ref, "inline": True},
            {"name": "Audit Details", "value": details.strip(), "inline": False},
            {"name": "Detected At", "value": detected_at_str, "inline": False},
        ]

        embed = {
            "title": f"{badge} DISCIPLINE AUDIT VIOLATION",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Mistake Detector"},
        }
        return embed

    def format_graduation_status_embed(
        self,
        status: str,
        total_trades: int,
        win_rate_pct: float,
        expectancy_pkr: float,
        max_drawdown_pct: float,
        blockers_or_status: str,
    ) -> Dict[str, Any]:
        """Format graduation eligibility change embed."""
        validate_graduation_status(status, total_trades, win_rate_pct, expectancy_pkr, max_drawdown_pct, blockers_or_status)

        is_grad = "GRADUATED" in status.upper()
        color = COLOR_PURPLE if is_grad else COLOR_BLUE
        emoji = "🎓" if is_grad else "📊"

        fields = [
            {"name": "Official Status", "value": f"**{status.upper()}**", "inline": True},
            {"name": "Total Trades", "value": f"{total_trades} (Req: >=30)", "inline": True},
            {"name": "Win Rate", "value": f"{win_rate_pct:.1f}%", "inline": True},
            {"name": "Expectancy", "value": f"PKR {expectancy_pkr:>+10,.2f}", "inline": True},
            {"name": "Max Drawdown", "value": f"{max_drawdown_pct:.2f}% (Limit: 10.0%)", "inline": True},
            {"name": "Analysis & Details", "value": blockers_or_status.strip(), "inline": False},
        ]

        embed = {
            "title": f"{emoji} GRADUATION STATUS UPDATE",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Graduation Engine"},
        }
        return embed

    def format_system_health_alert_embed(
        self,
        status: str,
        reason: str,
        affected_components: List[str],
        timestamp_str: str,
    ) -> Dict[str, Any]:
        """Format critical system outage / heartbeat silence alert embed."""
        validate_system_health_alert(status, reason, affected_components, timestamp_str)

        is_down = any(k in status.upper() for k in ("DOWN", "CRITICAL", "RED", "DEGRADED"))
        color = COLOR_RED if is_down else COLOR_GREEN
        comps = ", ".join(f"`{c}`" for c in affected_components)

        fields = [
            {"name": "Alert Condition", "value": reason.strip(), "inline": False},
            {"name": "Impacted Subsystems", "value": comps, "inline": False},
            {"name": "Threshold", "value": "Missed heartbeats > 2 minutes (120s)", "inline": True},
            {"name": "Reported At", "value": timestamp_str, "inline": True},
        ]

        embed = {
            "title": f"🚨 SYSTEM ALERT — {status.upper()}",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Health Monitor"},
        }
        return embed

    def format_daily_brief_embed(
        self,
        date_str: str,
        market_overview: str,
        watchlist_summary: List[Dict[str, Any]],
        key_levels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Format daily pre-market briefing embed (9:15 AM PKT)."""
        validate_daily_brief(date_str, market_overview)

        wl_lines = []
        for item in watchlist_summary:
            ticker = item.get("ticker", "N/A")
            last_price = item.get("price", 0.0)
            change_pct = item.get("change_pct", 0.0)
            sign = "+" if change_pct >= 0 else ""
            wl_lines.append(f"• `{ticker:<6}`: PKR {last_price:>7.2f} ({sign}{change_pct:.2f}%)")
        wl_text = "\n".join(wl_lines) if wl_lines else "• Focus symbols under observation."

        fields = [
            {"name": "Market Overview", "value": market_overview.strip(), "inline": False},
            {"name": "Focus Watchlist", "value": wl_text, "inline": False},
        ]

        if key_levels:
            fields.append({
                "name": "Key Support / Resistance",
                "value": "\n".join(f"• {l}" for l in key_levels),
                "inline": False,
            })

        fields.append({
            "name": "Discipline Rule",
            "value": "Max 1.0% risk/trade | Entry cutoff: 15:00 PKT",
            "inline": False,
        })

        embed = {
            "title": f"🌅 VETERANDESK DAILY BRIEF — {date_str}",
            "color": COLOR_BLUE,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • Morning Brief"},
        }
        return embed

    def format_session_summary_embed(
        self,
        session_date: str,
        trades_count: int,
        winning_trades: int,
        losing_trades: int,
        gross_pnl: float,
        total_fees: float,
        net_pnl: float,
        discipline_violations: int = 0,
        ending_cash: float = 500000.0,
    ) -> Dict[str, Any]:
        """Format post-market end-of-session summary embed (3:45 PM PKT)."""
        validate_session_summary(session_date, trades_count, winning_trades, losing_trades, gross_pnl, total_fees, net_pnl)

        win_rate = (winning_trades / trades_count * 100.0) if trades_count > 0 else 0.0
        color = COLOR_GREEN if net_pnl >= 0 else COLOR_RED
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
        sign = "+" if net_pnl >= 0 else ""

        fields = [
            {"name": "Trades Executed", "value": f"`{trades_count}` (Cap: 3)", "inline": True},
            {"name": "Win / Loss", "value": f"`{winning_trades}W / {losing_trades}L` ({win_rate:.1f}%)", "inline": True},
            {"name": "Discipline Breaches", "value": f"`{discipline_violations}` {'✅ (Clean)' if discipline_violations == 0 else '⚠️ (Flagged)'}", "inline": True},
            {"name": "Gross P&L", "value": f"PKR {gross_pnl:>+10,.2f}", "inline": True},
            {"name": "Fees & Taxes", "value": f"PKR {total_fees:>10,.2f}", "inline": True},
            {"name": "Net P&L", "value": f"{pnl_emoji} PKR {sign}{net_pnl:>+10,.2f}".strip(), "inline": True},
            {"name": "Closing Cash", "value": f"PKR {ending_cash:>10,.2f}", "inline": True},
        ]

        embed = {
            "title": f"🔔 VETERANDESK SESSION SUMMARY — {session_date}",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "VeteranDesk PSX Trading Agent • End of Session"},
        }
        return embed

    # =========================================================================
    # DATABASE DELIVERY TRACKING PERSISTENCE
    # =========================================================================

    def _persist_message_state(self, msg: DiscordOutboundMessage) -> None:
        """Persist message delivery status into Supabase or local SQLite."""
        row = {
            "id": msg.id,
            "message_type": msg.msg_type.value,
            "status": msg.status.value,
            "attempts": msg.attempts,
            "reference_id": msg.reference_id,
            "event_type": msg.event_type,
            "payload": msg.payload_json,
            "last_error": msg.last_error,
            "created_at": msg.created_at.isoformat(),
            "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
            "failed_at": msg.failed_at.isoformat() if msg.failed_at else None,
        }

        # 1. Try live Supabase PostgreSQL
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            client.table("discord_delivery_log").upsert(row, on_conflict="id").execute()
            return
        except Exception:
            pass

        # 2. SQLite local fallback
        try:
            from veterandesk.database.session import db_manager
            engine = db_manager.get_engine()
            with engine.connect() as conn:
                stmt = text("""
                    INSERT INTO discord_delivery_log (
                        id, message_type, status, attempts, reference_id,
                        event_type, payload, last_error, created_at, sent_at, failed_at
                    ) VALUES (
                        :id, :message_type, :status, :attempts, :reference_id,
                        :event_type, :payload, :last_error, :created_at, :sent_at, :failed_at
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        status = excluded.status,
                        attempts = excluded.attempts,
                        last_error = excluded.last_error,
                        sent_at = excluded.sent_at,
                        failed_at = excluded.failed_at
                """)
                conn.execute(stmt, row)
                conn.commit()
        except Exception as e:
            logger.warning("discord_db_persistence_failed", msg_id=msg.id, error=str(e))

    # =========================================================================
    # DELIVERY ENGINE (ASYNC & SYNC WITH RETRY, BACKOFF & RATE-LIMITING)
    # =========================================================================

    def enqueue_message(
        self,
        msg_type: MessageType,
        content: Optional[str] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        reference_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> DiscordOutboundMessage:
        """Enqueue message for delivery and persist initial pending record in DB."""
        msg = DiscordOutboundMessage(
            id=str(uuid.uuid4()),
            msg_type=msg_type,
            content=content,
            embeds=embeds or [],
            reference_id=reference_id,
            event_type=event_type,
            status=DeliveryStatus.PENDING,
        )
        self.outbound_queue.append(msg)
        self._persist_message_state(msg)
        logger.info("discord_message_queued", msg_id=msg.id, type=msg.msg_type.value, ref=reference_id)
        return msg

    async def _send_with_retry_async(self, msg: DiscordOutboundMessage) -> bool:
        """Deliver single message with exponential backoff and 429 rate limit respect (Async)."""
        if not self.enabled or not self.webhook_url or not self.webhook_url.strip():
            msg.is_delivered = False
            msg.status = DeliveryStatus.SKIPPED
            msg.sent_at = None
            msg.last_error = "Discord delivery skipped: notifier is disabled or unconfigured (DISCORD_WEBHOOK_URL missing or empty)"
            self._persist_message_state(msg)
            self.skipped_history.append(msg)
            logger.warning(
                "discord_delivery_skipped_not_configured",
                msg_id=msg.id,
                type=msg.msg_type.value,
                enabled=self.enabled,
                has_webhook=bool(self.webhook_url),
            )
            return False

        backoff_delays = [0.5, 1.0, 2.0]

        async with httpx.AsyncClient(timeout=10.0) as client:
            while msg.attempts < msg.max_attempts:
                msg.attempts += 1
                try:
                    resp = await client.post(self.webhook_url, json=msg.payload_dict)
                    self.last_send_timestamp = time.time()

                    # Discord webhook success is 200 or 204
                    if resp.status_code in (200, 204):
                        msg.is_delivered = True
                        msg.status = DeliveryStatus.SENT
                        msg.sent_at = datetime.now(timezone.utc)
                        self._persist_message_state(msg)
                        self.delivered_history.append(msg)
                        logger.info("discord_delivered_success", msg_id=msg.id, type=msg.msg_type.value, attempts=msg.attempts)
                        return True
                    elif resp.status_code == 429:
                        # Rate limit encountered
                        retry_after = 1.0
                        try:
                            rate_data = resp.json()
                            retry_after = float(rate_data.get("retry_after", 1.0))
                        except Exception:
                            pass
                        msg.last_error = f"HTTP 429 Rate Limited (retry_after={retry_after}s)"
                        logger.warning("discord_rate_limited", msg_id=msg.id, attempt=msg.attempts, retry_after=retry_after)
                        if msg.attempts < msg.max_attempts:
                            await asyncio.sleep(retry_after)
                            continue
                    else:
                        msg.last_error = f"HTTP {resp.status_code}: {resp.text}"
                        logger.warning("discord_send_failed_attempt", msg_id=msg.id, attempt=msg.attempts, error=msg.last_error)
                except Exception as e:
                    self.last_send_timestamp = time.time()
                    msg.last_error = str(e)
                    logger.warning("discord_send_exception_attempt", msg_id=msg.id, attempt=msg.attempts, error=str(e))

                if msg.attempts < msg.max_attempts:
                    delay = backoff_delays[min(msg.attempts - 1, len(backoff_delays) - 1)]
                    await asyncio.sleep(delay)

        # All attempts exhausted
        msg.status = DeliveryStatus.FAILED
        msg.failed_at = datetime.now(timezone.utc)
        self._persist_message_state(msg)
        self.failed_dead_letter.append(msg)
        logger.error(
            "discord_delivery_failed_permanently",
            msg_id=msg.id,
            type=msg.msg_type.value,
            attempts=msg.attempts,
            error=msg.last_error,
            ref=msg.reference_id,
        )
        return False

    def _send_with_retry_sync(self, msg: DiscordOutboundMessage) -> bool:
        """Deliver single message with exponential backoff and 429 rate limit respect (Sync)."""
        if not self.enabled or not self.webhook_url or not self.webhook_url.strip():
            msg.is_delivered = False
            msg.status = DeliveryStatus.SKIPPED
            msg.sent_at = None
            msg.last_error = "Discord delivery skipped: notifier is disabled or unconfigured (DISCORD_WEBHOOK_URL missing or empty)"
            self._persist_message_state(msg)
            self.skipped_history.append(msg)
            logger.warning(
                "discord_delivery_skipped_not_configured",
                msg_id=msg.id,
                type=msg.msg_type.value,
                enabled=self.enabled,
                has_webhook=bool(self.webhook_url),
            )
            return False

        backoff_delays = [0.5, 1.0, 2.0]

        with httpx.Client(timeout=10.0) as client:
            while msg.attempts < msg.max_attempts:
                msg.attempts += 1
                try:
                    resp = client.post(self.webhook_url, json=msg.payload_dict)
                    self.last_send_timestamp = time.time()

                    # Discord webhook success is 200 or 204
                    if resp.status_code in (200, 204):
                        msg.is_delivered = True
                        msg.status = DeliveryStatus.SENT
                        msg.sent_at = datetime.now(timezone.utc)
                        self._persist_message_state(msg)
                        self.delivered_history.append(msg)
                        logger.info("discord_delivered_success", msg_id=msg.id, type=msg.msg_type.value, attempts=msg.attempts)
                        return True
                    elif resp.status_code == 429:
                        # Rate limit encountered
                        retry_after = 1.0
                        try:
                            rate_data = resp.json()
                            retry_after = float(rate_data.get("retry_after", 1.0))
                        except Exception:
                            pass
                        msg.last_error = f"HTTP 429 Rate Limited (retry_after={retry_after}s)"
                        logger.warning("discord_rate_limited", msg_id=msg.id, attempt=msg.attempts, retry_after=retry_after)
                        if msg.attempts < msg.max_attempts:
                            time.sleep(retry_after)
                            continue
                    else:
                        msg.last_error = f"HTTP {resp.status_code}: {resp.text}"
                        logger.warning("discord_send_failed_attempt", msg_id=msg.id, attempt=msg.attempts, error=msg.last_error)
                except Exception as e:
                    self.last_send_timestamp = time.time()
                    msg.last_error = str(e)
                    logger.warning("discord_send_exception_attempt", msg_id=msg.id, attempt=msg.attempts, error=str(e))

                if msg.attempts < msg.max_attempts:
                    delay = backoff_delays[min(msg.attempts - 1, len(backoff_delays) - 1)]
                    time.sleep(delay)

        # All attempts exhausted
        msg.status = DeliveryStatus.FAILED
        msg.failed_at = datetime.now(timezone.utc)
        self._persist_message_state(msg)
        self.failed_dead_letter.append(msg)
        logger.error(
            "discord_delivery_failed_permanently",
            msg_id=msg.id,
            type=msg.msg_type.value,
            attempts=msg.attempts,
            error=msg.last_error,
            ref=msg.reference_id,
        )
        return False

    async def process_queue(self) -> int:
        """Process outbound queue with retries asynchronously."""
        if not self.outbound_queue:
            return 0

        delivered_count = 0
        remaining_queue: List[DiscordOutboundMessage] = []

        for msg in list(self.outbound_queue):
            success = await self._send_with_retry_async(msg)
            if success:
                delivered_count += 1
            elif msg.status not in (DeliveryStatus.FAILED, DeliveryStatus.SKIPPED):
                remaining_queue.append(msg)

        self.outbound_queue = remaining_queue
        return delivered_count

    def process_queue_sync(self) -> int:
        """Process outbound queue with retries synchronously."""
        if not self.outbound_queue:
            return 0

        delivered_count = 0
        remaining_queue: List[DiscordOutboundMessage] = []

        for msg in list(self.outbound_queue):
            success = self._send_with_retry_sync(msg)
            if success:
                delivered_count += 1
            elif msg.status not in (DeliveryStatus.FAILED, DeliveryStatus.SKIPPED):
                remaining_queue.append(msg)

        self.outbound_queue = remaining_queue
        return delivered_count

    # =========================================================================
    # HIGH-LEVEL ALERT DISPATCHERS (SYNC & ASYNC)
    # =========================================================================

    def send_message(
        self,
        content: Optional[str] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        msg_type: MessageType = MessageType.ALERT,
        reference_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> bool:
        """Send an arbitrary message or embed to Discord."""
        if not content and not embeds:
            raise ValueError("Must provide either content or embeds for Discord message")
        msg = self.enqueue_message(
            msg_type=msg_type,
            content=content,
            embeds=embeds,
            reference_id=reference_id,
            event_type=event_type or "GENERAL_ALERT",
        )
        return self._send_with_retry_sync(msg)

    def send_signal_alert(self, signal: TradeSignal, shares: int, reason_lines: str) -> bool:
        """Format and dispatch trade signal notification embed."""
        embed = self.format_signal_embed(signal, shares, reason_lines)
        msg = self.enqueue_message(
            msg_type=MessageType.SIGNAL,
            embeds=[embed],
            reference_id=signal.signal_id,
            event_type="NEW_SIGNAL_APPROVED",
        )
        return self._send_with_retry_sync(msg)

    def send_level_hit_alert(
        self,
        ticker: str,
        trade_id: str,
        level_type: str,
        price: float,
        fill_price: float,
        net_pnl: float,
        closed_at_str: Optional[str] = None,
    ) -> bool:
        """Format and dispatch level hit notification embed."""
        ts_str = closed_at_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        embed = self.format_level_hit_embed(
            ticker=ticker,
            trade_id=trade_id,
            level_type=level_type,
            price=price,
            fill_price=fill_price,
            net_pnl=net_pnl,
            closed_at_str=ts_str,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.LEVEL_HIT,
            embeds=[embed],
            reference_id=trade_id,
            event_type=f"LEVEL_HIT_{level_type}",
        )
        return self._send_with_retry_sync(msg)

    def send_daily_halt_alert(
        self,
        loss_pct: float,
        max_loss_pct: float,
        loss_amount_pkr: float,
        halt_time_pkt: Optional[str] = None,
        action_taken: str = "Trading halted; all open positions flattened.",
    ) -> bool:
        """Format and dispatch daily loss halt notification embed."""
        t_str = halt_time_pkt or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        embed = self.format_daily_loss_halt_embed(
            loss_pct=loss_pct,
            max_loss_pct=max_loss_pct,
            loss_amount_pkr=loss_amount_pkr,
            halt_time_pkt=t_str,
            action_taken=action_taken,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.DAILY_HALT,
            embeds=[embed],
            reference_id=f"HALT_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            event_type="DAILY_LOSS_HALT_TRIGGERED",
        )
        return self._send_with_retry_sync(msg)

    def send_mistake_alert(
        self,
        rule_violated: str,
        severity: str,
        trade_id: Optional[str],
        details: str,
        detected_at_str: Optional[str] = None,
    ) -> bool:
        """Format and dispatch mistake audit alert embed."""
        ts_str = detected_at_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        embed = self.format_mistake_alert_embed(
            rule_violated=rule_violated,
            severity=severity,
            trade_id=trade_id,
            details=details,
            detected_at_str=ts_str,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.MISTAKE_VIOLATION,
            embeds=[embed],
            reference_id=trade_id,
            event_type=f"MISTAKE_{rule_violated}",
        )
        return self._send_with_retry_sync(msg)

    def send_graduation_alert(
        self,
        status: str,
        total_trades: int,
        win_rate_pct: float,
        expectancy_pkr: float,
        max_drawdown_pct: float,
        blockers_or_status: str,
    ) -> bool:
        """Format and dispatch graduation status change embed."""
        embed = self.format_graduation_status_embed(
            status=status,
            total_trades=total_trades,
            win_rate_pct=win_rate_pct,
            expectancy_pkr=expectancy_pkr,
            max_drawdown_pct=max_drawdown_pct,
            blockers_or_status=blockers_or_status,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.GRADUATION_STATUS,
            embeds=[embed],
            reference_id=f"GRADUATION_{int(time.time())}",
            event_type="GRADUATION_STATUS_CHANGE",
        )
        return self._send_with_retry_sync(msg)

    def send_system_health_alert(
        self,
        status: str,
        reason: str,
        affected_components: List[str],
        timestamp_str: Optional[str] = None,
    ) -> bool:
        """Format and dispatch system health outage alert embed."""
        ts_str = timestamp_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        embed = self.format_system_health_alert_embed(
            status=status,
            reason=reason,
            affected_components=affected_components,
            timestamp_str=ts_str,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.SYSTEM_HEALTH,
            embeds=[embed],
            reference_id=f"HEALTH_{int(time.time())}",
            event_type="SYSTEM_HEALTH_OUTAGE",
        )
        return self._send_with_retry_sync(msg)

    def send_daily_brief(
        self,
        date_str: str,
        market_overview: str,
        watchlist_summary: List[Dict[str, Any]],
        key_levels: Optional[List[str]] = None,
    ) -> bool:
        """Format and dispatch daily morning briefing embed."""
        embed = self.format_daily_brief_embed(
            date_str=date_str,
            market_overview=market_overview,
            watchlist_summary=watchlist_summary,
            key_levels=key_levels,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.DAILY_BRIEF,
            embeds=[embed],
            reference_id=f"BRIEF_{date_str}",
            event_type="DAILY_BRIEF_SCHEDULED",
        )
        return self._send_with_retry_sync(msg)

    def send_session_summary(
        self,
        session_date: str,
        trades_count: int,
        winning_trades: int,
        losing_trades: int,
        gross_pnl: float,
        total_fees: float,
        net_pnl: float,
        discipline_violations: int = 0,
        ending_cash: float = 500000.0,
    ) -> bool:
        """Format and dispatch end-of-session summary embed."""
        embed = self.format_session_summary_embed(
            session_date=session_date,
            trades_count=trades_count,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            discipline_violations=discipline_violations,
            ending_cash=ending_cash,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.SESSION_SUMMARY,
            embeds=[embed],
            reference_id=f"SUMMARY_{session_date}",
            event_type="SESSION_SUMMARY_SCHEDULED",
        )
        return self._send_with_retry_sync(msg)


# Global singleton instance
discord_service = DiscordService()


def get_discord_delivery_stats() -> Dict[str, int]:
    """Retrieve Discord message delivery metrics from database."""
    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()
        res = client.table("discord_delivery_log").select("status").execute()
        rows = res.data or []
        total = len(rows)
        sent = sum(1 for r in rows if r.get("status") == "sent")
        failed = sum(1 for r in rows if r.get("status") == "failed")
        pending = sum(1 for r in rows if r.get("status") == "pending")
        skipped = sum(1 for r in rows if r.get("status") == "skipped")
        return {"total": total, "sent": sent, "failed": failed, "pending": pending, "skipped": skipped}
    except Exception:
        pass

    try:
        from veterandesk.database.session import db_manager
        engine = db_manager.get_engine()
        with engine.connect() as conn:
            res = conn.execute(text("SELECT status, COUNT(*) FROM discord_delivery_log GROUP BY status")).fetchall()
            counts = {r[0]: r[1] for r in res}
            total = sum(counts.values())
            return {
                "total": total,
                "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0),
                "pending": counts.get("pending", 0),
                "skipped": counts.get("skipped", 0),
            }
    except Exception:
        return {"total": 0, "sent": 0, "failed": 0, "pending": 0, "skipped": 0}
