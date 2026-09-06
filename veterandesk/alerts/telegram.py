"""
Telegram Alerts and Signal Broadcast Service.

Robustness features:
1. Outbound delivery with exponential backoff and retry (up to 3 attempts).
2. Schema-validated message templates (strictly prevents None or blank renders).
3. Persistent DB delivery tracking in `telegram_delivery_log` (pending -> sent / failed).
4. Rate-limit aware throttling (max 1 msg/sec per chat).
5. Async and synchronous delivery support.
6. Graceful offline/disabled mode when credentials are not configured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
import time
from typing import Any, Dict, List, Optional
import uuid
import httpx
from sqlalchemy import text

from veterandesk.config import settings
from veterandesk.logging import get_logger
from veterandesk.strategy.models import TradeSignal

logger = get_logger("veterandesk.telegram")


class MessageType(str, Enum):
    SIGNAL = "SIGNAL"
    LEVEL_HIT = "LEVEL_HIT"
    DAILY_HALT = "DAILY_HALT"
    MISTAKE_VIOLATION = "MISTAKE_VIOLATION"
    GRADUATION_STATUS = "GRADUATION_STATUS"
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    DAILY_BRIEF = "DAILY_BRIEF"
    SESSION_SUMMARY = "SESSION_SUMMARY"
    ALERT = "ALERT"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class OutboundMessage:
    id: str
    msg_type: MessageType
    text: str
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


class TelegramService:
    """
    Robust Telegram notification service with retry with backoff, rate limiting,
    schema-validated templates, and persistent database delivery tracking.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        # Read strictly from environment variables or settings
        self.bot_token = bot_token or settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or settings.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        
        env_enabled = settings.telegram_enabled
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = bool(env_enabled and self.bot_token and self.chat_id)

        self.outbound_queue: List[OutboundMessage] = []
        self.delivered_history: List[OutboundMessage] = []
        self.failed_dead_letter: List[OutboundMessage] = []
        self.last_send_timestamp: float = 0.0
        self.min_interval_seconds: float = 1.0  # Respect 1 msg/sec Telegram rate limit

    # =========================================================================
    # MESSAGE TEMPLATES (SCHEMA-VALIDATED — ZERO NONE / BLANK RENDERS)
    # =========================================================================

    def format_signal_message(self, signal: TradeSignal, shares: int, reason_lines: str) -> str:
        """Format and validate trade signal notification."""
        if not signal.ticker or not str(signal.ticker).strip():
            raise ValueError("Signal ticker cannot be empty or None")
        if signal.entry_price is None or signal.entry_price <= 0:
            raise ValueError(f"Signal entry_price invalid: {signal.entry_price}")
        if signal.stop_loss is None or signal.stop_loss <= 0:
            raise ValueError(f"Signal stop_loss invalid: {signal.stop_loss}")
        if signal.target_price is None or signal.target_price <= 0:
            raise ValueError(f"Signal target_price invalid: {signal.target_price}")
        if shares is None or shares <= 0:
            raise ValueError(f"Signal shares count invalid: {shares}")
        if not reason_lines or not reason_lines.strip():
            raise ValueError("Signal reason_lines cannot be empty or None")

        text = (
            f"🎯 *VETERANDESK TRADE SIGNAL*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• *Ticker:* `{signal.ticker.upper()}`\n"
            f"• *Action:* `{signal.action.value}`\n"
            f"• *Quantity:* `{shares:,} shares`\n"
            f"• *Entry:* `PKR {signal.entry_price:.2f}`\n"
            f"• *Stop-Loss:* `PKR {signal.stop_loss:.2f}`\n"
            f"• *Target:* `PKR {signal.target_price:.2f}`\n"
            f"• *Reward/Risk:* `{signal.reward_risk_ratio:.2f}:1`\n"
            f"• *Confidence:* `{signal.confidence_pct}%`\n"
            f"• *Strategy:* `{signal.strategy}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📝 *Rationale:*\n{reason_lines.strip()}\n"
            f"⏱ _Time: {signal.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        return text

    def format_level_hit_message(
        self,
        ticker: str,
        trade_id: str,
        level_type: str,
        price: float,
        fill_price: float,
        net_pnl: float,
        closed_at_str: str,
    ) -> str:
        """Format level hit alert (Target, Stop, or Cutoff hit)."""
        if not ticker or not ticker.strip():
            raise ValueError("Ticker cannot be blank")
        if not trade_id or not trade_id.strip():
            raise ValueError("Trade ID cannot be blank")
        if not level_type or not level_type.strip():
            raise ValueError("Level type cannot be blank")
        if price is None or price <= 0:
            raise ValueError(f"Trigger price invalid: {price}")
        if fill_price is None or fill_price <= 0:
            raise ValueError(f"Fill price invalid: {fill_price}")
        if net_pnl is None:
            raise ValueError("Net PnL cannot be None")
        if not closed_at_str or not closed_at_str.strip():
            raise ValueError("Closed at timestamp cannot be blank")

        is_target = "TARGET" in level_type.upper()
        emoji = "🎯" if is_target else ("🛑" if "STOP" in level_type.upper() else "⏰")
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
        sign = "+" if net_pnl >= 0 else ""

        text = (
            f"{emoji} *VETERANDESK LEVEL HIT — {ticker.upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• *Event:* `{level_type}`\n"
            f"• *Trade ID:* `{trade_id}`\n"
            f"• *Trigger Price:* `PKR {price:.2f}`\n"
            f"• *Fill Price:* `PKR {fill_price:.2f}`\n"
            f"• *Realized Net PnL:* {pnl_emoji} `PKR {sign}{net_pnl:>+10,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ _Closed At: {closed_at_str}_"
        )
        return text

    def format_daily_loss_halt_message(
        self,
        loss_pct: float,
        max_loss_pct: float,
        loss_amount_pkr: float,
        halt_time_pkt: str,
        action_taken: str,
    ) -> str:
        """Format daily loss halt critical notification."""
        if loss_pct is None or loss_pct <= 0:
            raise ValueError(f"Loss pct invalid: {loss_pct}")
        if max_loss_pct is None or max_loss_pct <= 0:
            raise ValueError(f"Max loss pct invalid: {max_loss_pct}")
        if loss_amount_pkr is None or loss_amount_pkr <= 0:
            raise ValueError(f"Loss amount invalid: {loss_amount_pkr}")
        if not halt_time_pkt or not halt_time_pkt.strip():
            raise ValueError("Halt time cannot be blank")
        if not action_taken or not action_taken.strip():
            raise ValueError("Action taken cannot be blank")

        text = (
            f"🚨 *CRITICAL DISCIPLINE HALT TRIGGERED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Daily Loss Reached:* `{loss_pct:.2f}%` (Limit: `{max_loss_pct:.2f}%`)\n"
            f"• *Total Realized Loss:* `PKR {loss_amount_pkr:>+10,.2f}`\n"
            f"• *Trigger Time:* `{halt_time_pkt}`\n"
            f"• *Enforcement Action:* `{action_taken.strip()}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 _Discipline Invariant: All trading is blocked until the next market session._"
        )
        return text

    def format_mistake_alert_message(
        self,
        rule_violated: str,
        severity: str,
        trade_id: Optional[str],
        details: str,
        detected_at_str: str,
    ) -> str:
        """Format independent mistake detection audit alert."""
        if not rule_violated or not rule_violated.strip():
            raise ValueError("Rule violated cannot be blank")
        if not severity or not severity.strip():
            raise ValueError("Severity cannot be blank")
        if not details or not details.strip():
            raise ValueError("Details cannot be blank")
        if not detected_at_str or not detected_at_str.strip():
            raise ValueError("Detected at timestamp cannot be blank")

        trd_ref = f"`{trade_id.strip()}`" if trade_id and trade_id.strip() else "`N/A (System-wide)`"
        is_crit = severity.upper() == "CRITICAL"
        badge = "🚨 CRITICAL" if is_crit else "⚠️ WARNING"

        text = (
            f"{badge} *DISCIPLINE AUDIT VIOLATION DETECTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Rule Violated:* `{rule_violated}`\n"
            f"• *Severity:* `{severity.upper()}`\n"
            f"• *Trade Ref:* {trd_ref}\n"
            f"• *Audit Details:*\n{details.strip()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ _Detected: {detected_at_str}_"
        )
        return text

    def format_graduation_status_message(
        self,
        status: str,
        total_trades: int,
        win_rate_pct: float,
        expectancy_pkr: float,
        max_drawdown_pct: float,
        blockers_or_status: str,
    ) -> str:
        """Format graduation eligibility change alert."""
        if not status or not status.strip():
            raise ValueError("Status cannot be blank")
        if total_trades is None or total_trades < 0:
            raise ValueError("Total trades cannot be None or negative")
        if win_rate_pct is None or win_rate_pct < 0:
            raise ValueError("Win rate cannot be None or negative")
        if expectancy_pkr is None:
            raise ValueError("Expectancy cannot be None")
        if max_drawdown_pct is None or max_drawdown_pct < 0:
            raise ValueError("Max drawdown cannot be None or negative")
        if not blockers_or_status or not blockers_or_status.strip():
            raise ValueError("Blockers/status description cannot be blank")

        is_grad = "GRADUATED" in status.upper()
        emoji = "🎓" if is_grad else "📊"

        text = (
            f"{emoji} *VETERANDESK GRADUATION STATUS UPDATE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Official Status:* `{status.upper()}`\n"
            f"• *Total Trades:* `{total_trades}` (Req: >=30)\n"
            f"• *Win Rate:* `{win_rate_pct:.1f}%`\n"
            f"• *Mathematical Expectancy:* `PKR {expectancy_pkr:>+10,.2f}`\n"
            f"• *Max Drawdown:* `{max_drawdown_pct:.2f}%` (Max allowed: 10.0%)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *Graduation Analysis:*\n{blockers_or_status.strip()}\n"
            f"📌 _Real capital allocation allowed only upon meeting all mathematical criteria._"
        )
        return text

    def format_system_health_alert_message(
        self,
        status: str,
        reason: str,
        affected_components: List[str],
        timestamp_str: str,
    ) -> str:
        """Format critical system outage / heartbeat silence alert."""
        if not status or not status.strip():
            raise ValueError("Status cannot be blank")
        if not reason or not reason.strip():
            raise ValueError("Reason cannot be blank")
        if not affected_components:
            raise ValueError("Affected components cannot be empty")
        if not timestamp_str or not timestamp_str.strip():
            raise ValueError("Timestamp cannot be blank")

        comps = ", ".join(f"`{c}`" for c in affected_components)

        text = (
            f"🚨 *SYSTEM ALERT — {status.upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Alert Condition:* `{reason.strip()}`\n"
            f"• *Impacted Subsystems:* {comps}\n"
            f"• *Threshold:* Missed heartbeats > 2 minutes (120s)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ _Reported At: {timestamp_str}_\n"
            f"⚠️ _Check console logs or dashboard System Health page immediately._"
        )
        return text

    def format_daily_brief(
        self,
        date_str: str,
        market_overview: str,
        watchlist_summary: List[Dict[str, Any]],
        key_levels: Optional[List[str]] = None,
    ) -> str:
        """Format daily pre-market briefing (9:15 AM PKT)."""
        if not date_str or not date_str.strip():
            raise ValueError("Date string cannot be blank")
        if not market_overview or not market_overview.strip():
            raise ValueError("Market overview cannot be blank")

        wl_lines = []
        for item in watchlist_summary:
            ticker = item.get("ticker", "N/A")
            last_price = item.get("price", 0.0)
            change_pct = item.get("change_pct", 0.0)
            sign = "+" if change_pct >= 0 else ""
            wl_lines.append(f"  • `{ticker:<6}`: PKR {last_price:>7.2f} ({sign}{change_pct:.2f}%)")
        wl_text = "\n".join(wl_lines) if wl_lines else "  • Focus symbols under observation."

        levels_text = ""
        if key_levels:
            levels_text = "\n📍 *Key Support/Resistance:*\n" + "\n".join(f"  • {l}" for l in key_levels)

        text = (
            f"🌅 *VETERANDESK DAILY BRIEF — {date_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Market Overview:*\n{market_overview.strip()}\n\n"
            f"📋 *Focus Watchlist:*\n{wl_text}"
            f"{levels_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ _Discipline Rule: Max 1.0% risk/trade | Entry cutoff: 15:00 PKT_"
        )
        return text

    def format_session_summary(
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
    ) -> str:
        """Format post-market end-of-session summary (3:45 PM PKT)."""
        if not session_date or not session_date.strip():
            raise ValueError("Session date cannot be blank")
        if trades_count is None or trades_count < 0:
            raise ValueError("Trades count cannot be None or negative")
        if winning_trades is None or winning_trades < 0:
            raise ValueError("Winning trades cannot be None or negative")
        if losing_trades is None or losing_trades < 0:
            raise ValueError("Losing trades cannot be None or negative")
        if gross_pnl is None:
            raise ValueError("Gross PnL cannot be None")
        if total_fees is None or total_fees < 0:
            raise ValueError("Total fees cannot be None or negative")
        if net_pnl is None:
            raise ValueError("Net PnL cannot be None")

        win_rate = (winning_trades / trades_count * 100.0) if trades_count > 0 else 0.0
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
        sign = "+" if net_pnl >= 0 else ""

        text = (
            f"🔔 *VETERANDESK SESSION SUMMARY — {session_date}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Trades Executed:* `{trades_count}` (Cap: 3)\n"
            f"• *Win / Loss:* `{winning_trades}W / {losing_trades}L` (Win Rate: `{win_rate:.1f}%`)\n"
            f"• *Gross P&L:* `PKR {gross_pnl:>+10,.2f}`\n"
            f"• *Fees & Taxes Paid:* `PKR {total_fees:>10,.2f}`\n"
            f"• *Net P&L:* {pnl_emoji} `PKR {sign}{net_pnl:>10,.2f}`\n"
            f"• *Closing Cash:* `PKR {ending_cash:>10,.2f}`\n"
            f"• *Discipline Breaches:* `{discipline_violations}` {'✅ (Clean)' if discipline_violations == 0 else '⚠️ (Flagged)'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 _Status: All positions closed prior to 15:20 PKT force cutoff._"
        )
        return text

    # =========================================================================
    # DATABASE DELIVERY TRACKING PERSISTENCE
    # =========================================================================

    def _persist_message_state(self, msg: OutboundMessage) -> None:
        """Persist message delivery status into Supabase or local SQLite."""
        row = {
            "id": msg.id,
            "message_type": msg.msg_type.value,
            "status": msg.status.value,
            "attempts": msg.attempts,
            "reference_id": msg.reference_id,
            "event_type": msg.event_type,
            "payload": msg.text,
            "last_error": msg.last_error,
            "created_at": msg.created_at.isoformat(),
            "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
            "failed_at": msg.failed_at.isoformat() if msg.failed_at else None,
        }

        # 1. Try live Supabase PostgreSQL
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            client.table("telegram_delivery_log").upsert(row, on_conflict="id").execute()
            return
        except Exception:
            pass

        # 2. SQLite local fallback
        try:
            from veterandesk.database.session import db_manager
            engine = db_manager.get_engine()
            with engine.connect() as conn:
                stmt = text("""
                    INSERT INTO telegram_delivery_log (
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
            logger.warning("telegram_db_persistence_failed", msg_id=msg.id, error=str(e))

    # =========================================================================
    # DELIVERY ENGINE (ASYNC & SYNC WITH RETRY, BACKOFF & THROTTLING)
    # =========================================================================

    def enqueue_message(
        self,
        msg_type: MessageType,
        text: str,
        reference_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> OutboundMessage:
        """Enqueue message for delivery and persist initial pending record in DB."""
        msg = OutboundMessage(
            id=str(uuid.uuid4()),
            msg_type=msg_type,
            text=text,
            reference_id=reference_id,
            event_type=event_type,
            status=DeliveryStatus.PENDING,
        )
        self.outbound_queue.append(msg)
        self._persist_message_state(msg)
        logger.info("telegram_message_queued", msg_id=msg.id, type=msg.msg_type.value, ref=reference_id)
        return msg

    async def _send_with_retry_async(self, msg: OutboundMessage) -> bool:
        """Deliver single message with exponential backoff and rate limiting."""
        if not self.enabled or not self.bot_token or not self.chat_id:
            # Offline / Disabled mode: mark as sent/mock-delivered
            msg.is_delivered = True
            msg.status = DeliveryStatus.SENT
            msg.sent_at = datetime.now(timezone.utc)
            self._persist_message_state(msg)
            self.delivered_history.append(msg)
            logger.info("telegram_mock_delivered", msg_id=msg.id, type=msg.msg_type.value)
            return True

        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        backoff_delays = [0.5, 1.0, 2.0]

        async with httpx.AsyncClient(timeout=10.0) as client:
            while msg.attempts < msg.max_attempts:
                msg.attempts += 1

                # Rate limiting: throttle 1.0s between sends to same chat
                now = time.time()
                elapsed = now - self.last_send_timestamp
                if elapsed < self.min_interval_seconds:
                    await asyncio.sleep(self.min_interval_seconds - elapsed)

                try:
                    payload = {
                        "chat_id": self.chat_id,
                        "text": msg.text,
                        "parse_mode": "Markdown",
                    }
                    resp = await client.post(api_url, json=payload)
                    self.last_send_timestamp = time.time()

                    if resp.status_code == 200:
                        msg.is_delivered = True
                        msg.status = DeliveryStatus.SENT
                        msg.sent_at = datetime.now(timezone.utc)
                        self._persist_message_state(msg)
                        self.delivered_history.append(msg)
                        logger.info("telegram_delivered_success", msg_id=msg.id, type=msg.msg_type.value, attempts=msg.attempts)
                        return True
                    else:
                        msg.last_error = f"HTTP {resp.status_code}: {resp.text}"
                        logger.warning("telegram_send_failed_attempt", msg_id=msg.id, attempt=msg.attempts, error=msg.last_error)
                except Exception as e:
                    self.last_send_timestamp = time.time()
                    msg.last_error = str(e)
                    logger.warning("telegram_send_exception_attempt", msg_id=msg.id, attempt=msg.attempts, error=str(e))

                if msg.attempts < msg.max_attempts:
                    delay = backoff_delays[min(msg.attempts - 1, len(backoff_delays) - 1)]
                    await asyncio.sleep(delay)

        # All 3 attempts exhausted
        msg.status = DeliveryStatus.FAILED
        msg.failed_at = datetime.now(timezone.utc)
        self._persist_message_state(msg)
        self.failed_dead_letter.append(msg)
        logger.error(
            "telegram_delivery_failed_permanently",
            msg_id=msg.id,
            type=msg.msg_type.value,
            attempts=msg.attempts,
            error=msg.last_error,
            ref=msg.reference_id,
        )
        return False

    def _send_with_retry_sync(self, msg: OutboundMessage) -> bool:
        """Synchronous delivery with exponential backoff and rate limiting."""
        if not self.enabled or not self.bot_token or not self.chat_id:
            # Offline / Disabled mode: mark as sent/mock-delivered
            msg.is_delivered = True
            msg.status = DeliveryStatus.SENT
            msg.sent_at = datetime.now(timezone.utc)
            self._persist_message_state(msg)
            self.delivered_history.append(msg)
            logger.info("telegram_mock_delivered", msg_id=msg.id, type=msg.msg_type.value)
            return True

        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        backoff_delays = [0.5, 1.0, 2.0]

        with httpx.Client(timeout=10.0) as client:
            while msg.attempts < msg.max_attempts:
                msg.attempts += 1

                # Rate limiting: throttle 1.0s between sends to same chat
                now = time.time()
                elapsed = now - self.last_send_timestamp
                if elapsed < self.min_interval_seconds:
                    time.sleep(self.min_interval_seconds - elapsed)

                try:
                    payload = {
                        "chat_id": self.chat_id,
                        "text": msg.text,
                        "parse_mode": "Markdown",
                    }
                    resp = client.post(api_url, json=payload)
                    self.last_send_timestamp = time.time()

                    if resp.status_code == 200:
                        msg.is_delivered = True
                        msg.status = DeliveryStatus.SENT
                        msg.sent_at = datetime.now(timezone.utc)
                        self._persist_message_state(msg)
                        self.delivered_history.append(msg)
                        logger.info("telegram_delivered_success", msg_id=msg.id, type=msg.msg_type.value, attempts=msg.attempts)
                        return True
                    else:
                        msg.last_error = f"HTTP {resp.status_code}: {resp.text}"
                        logger.warning("telegram_send_failed_attempt", msg_id=msg.id, attempt=msg.attempts, error=msg.last_error)
                except Exception as e:
                    self.last_send_timestamp = time.time()
                    msg.last_error = str(e)
                    logger.warning("telegram_send_exception_attempt", msg_id=msg.id, attempt=msg.attempts, error=str(e))

                if msg.attempts < msg.max_attempts:
                    delay = backoff_delays[min(msg.attempts - 1, len(backoff_delays) - 1)]
                    time.sleep(delay)

        # All 3 attempts exhausted
        msg.status = DeliveryStatus.FAILED
        msg.failed_at = datetime.now(timezone.utc)
        self._persist_message_state(msg)
        self.failed_dead_letter.append(msg)
        logger.error(
            "telegram_delivery_failed_permanently",
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
        remaining_queue: List[OutboundMessage] = []

        for msg in list(self.outbound_queue):
            success = await self._send_with_retry_async(msg)
            if success:
                delivered_count += 1
            else:
                remaining_queue.append(msg)

        self.outbound_queue = remaining_queue
        return delivered_count

    def process_queue_sync(self) -> int:
        """Process outbound queue with retries synchronously."""
        if not self.outbound_queue:
            return 0

        delivered_count = 0
        remaining_queue: List[OutboundMessage] = []

        for msg in list(self.outbound_queue):
            success = self._send_with_retry_sync(msg)
            if success:
                delivered_count += 1
            else:
                remaining_queue.append(msg)

        self.outbound_queue = remaining_queue
        return delivered_count

    # =========================================================================
    # HIGH-LEVEL ALERT DISPATCHERS (ASYNC & SYNC)
    # =========================================================================

    def send_message(
        self,
        text: str,
        msg_type: MessageType = MessageType.ALERT,
        reference_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> bool:
        """Send an arbitrary schema-validated message with retry and persistence."""
        if not text or not text.strip():
            raise ValueError("Message text cannot be empty or None")
        msg = self.enqueue_message(
            msg_type=msg_type,
            text=text,
            reference_id=reference_id,
            event_type=event_type or "GENERAL_ALERT",
        )
        return self._send_with_retry_sync(msg)

    def send_signal_alert(self, signal: TradeSignal, shares: int, reason_lines: str) -> bool:
        """Format and immediately dispatch new signal alert."""
        text = self.format_signal_message(signal, shares, reason_lines)
        msg = self.enqueue_message(
            msg_type=MessageType.SIGNAL,
            text=text,
            reference_id=signal.signal_id,
            event_type="NEW_SIGNAL_APPROVED"
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
        """Format and immediately dispatch level hit notification."""
        ts_str = closed_at_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = self.format_level_hit_message(
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
            text=text,
            reference_id=trade_id,
            event_type=f"LEVEL_HIT_{level_type}"
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
        """Format and immediately dispatch daily loss limit halt alert."""
        t_str = halt_time_pkt or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = self.format_daily_loss_halt_message(
            loss_pct=loss_pct,
            max_loss_pct=max_loss_pct,
            loss_amount_pkr=loss_amount_pkr,
            halt_time_pkt=t_str,
            action_taken=action_taken,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.DAILY_HALT,
            text=text,
            reference_id=f"HALT_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            event_type="DAILY_LOSS_HALT_TRIGGERED"
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
        """Format and immediately dispatch mistake audit alert."""
        ts_str = detected_at_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = self.format_mistake_alert_message(
            rule_violated=rule_violated,
            severity=severity,
            trade_id=trade_id,
            details=details,
            detected_at_str=ts_str,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.MISTAKE_VIOLATION,
            text=text,
            reference_id=trade_id,
            event_type=f"MISTAKE_{rule_violated}"
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
        """Format and immediately dispatch graduation status change alert."""
        text = self.format_graduation_status_message(
            status=status,
            total_trades=total_trades,
            win_rate_pct=win_rate_pct,
            expectancy_pkr=expectancy_pkr,
            max_drawdown_pct=max_drawdown_pct,
            blockers_or_status=blockers_or_status,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.GRADUATION_STATUS,
            text=text,
            reference_id=f"GRADUATION_{int(time.time())}",
            event_type="GRADUATION_STATUS_CHANGE"
        )
        return self._send_with_retry_sync(msg)

    def send_system_health_alert(
        self,
        status: str,
        reason: str,
        affected_components: List[str],
        timestamp_str: Optional[str] = None,
    ) -> bool:
        """Format and immediately dispatch critical system health outage alert."""
        ts_str = timestamp_str or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = self.format_system_health_alert_message(
            status=status,
            reason=reason,
            affected_components=affected_components,
            timestamp_str=ts_str,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.SYSTEM_HEALTH,
            text=text,
            reference_id=f"HEALTH_{int(time.time())}",
            event_type="SYSTEM_HEALTH_OUTAGE"
        )
        return self._send_with_retry_sync(msg)

    def send_daily_brief(
        self,
        date_str: str,
        market_overview: str,
        watchlist_summary: List[Dict[str, Any]],
        key_levels: Optional[List[str]] = None,
    ) -> bool:
        """Format and immediately dispatch daily morning briefing."""
        text = self.format_daily_brief(
            date_str=date_str,
            market_overview=market_overview,
            watchlist_summary=watchlist_summary,
            key_levels=key_levels,
        )
        msg = self.enqueue_message(
            msg_type=MessageType.DAILY_BRIEF,
            text=text,
            reference_id=f"BRIEF_{date_str}",
            event_type="DAILY_BRIEF_SCHEDULED"
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
        """Format and immediately dispatch end-of-session summary."""
        text = self.format_session_summary(
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
            text=text,
            reference_id=f"SUMMARY_{session_date}",
            event_type="SESSION_SUMMARY_SCHEDULED"
        )
        return self._send_with_retry_sync(msg)


# Global singleton instance
telegram_service = TelegramService()


def get_delivery_stats() -> Dict[str, int]:
    """Retrieve message delivery metrics from database."""
    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()
        res = client.table("telegram_delivery_log").select("status").execute()
        rows = res.data or []
        total = len(rows)
        sent = sum(1 for r in rows if r.get("status") == "sent")
        failed = sum(1 for r in rows if r.get("status") == "failed")
        pending = sum(1 for r in rows if r.get("status") == "pending")
        return {"total": total, "sent": sent, "failed": failed, "pending": pending}
    except Exception:
        pass

    try:
        from veterandesk.database.session import db_manager
        engine = db_manager.get_engine()
        with engine.connect() as conn:
            res = conn.execute(text("SELECT status, COUNT(*) FROM telegram_delivery_log GROUP BY status")).fetchall()
            counts = {r[0]: r[1] for r in res}
            total = sum(counts.values())
            return {
                "total": total,
                "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0),
                "pending": counts.get("pending", 0),
            }
    except Exception:
        return {"total": 0, "sent": 0, "failed": 0, "pending": 0}
