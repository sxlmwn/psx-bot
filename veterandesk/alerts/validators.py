"""
Shared schema validation functions for VeteranDesk alerts and notifications.

Guarantees:
1. Zero empty or None fields rendered across all delivery channels (Telegram, Discord, etc.).
2. Single source of truth for validation rules preventing duplicated logic.
"""

from __future__ import annotations

from typing import Any, List, Optional
from veterandesk.strategy.models import TradeSignal


def validate_signal(signal: TradeSignal, shares: int, reason_lines: str) -> None:
    """Validate trade signal parameters."""
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


def validate_level_hit(
    ticker: str,
    trade_id: str,
    level_type: str,
    price: float,
    fill_price: float,
    net_pnl: float,
    closed_at_str: str,
) -> None:
    """Validate level hit parameters."""
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


def validate_daily_loss_halt(
    loss_pct: float,
    max_loss_pct: float,
    loss_amount_pkr: float,
    halt_time_pkt: str,
    action_taken: str,
) -> None:
    """Validate daily loss halt parameters."""
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


def validate_mistake_alert(
    rule_violated: str,
    severity: str,
    details: str,
    detected_at_str: str,
) -> None:
    """Validate mistake alert parameters."""
    if not rule_violated or not rule_violated.strip():
        raise ValueError("Rule violated cannot be blank")
    if not severity or not severity.strip():
        raise ValueError("Severity cannot be blank")
    if not details or not details.strip():
        raise ValueError("Details cannot be blank")
    if not detected_at_str or not detected_at_str.strip():
        raise ValueError("Detected at timestamp cannot be blank")


def validate_graduation_status(
    status: str,
    total_trades: int,
    win_rate_pct: float,
    expectancy_pkr: float,
    max_drawdown_pct: float,
    blockers_or_status: str,
) -> None:
    """Validate graduation status parameters."""
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


def validate_system_health_alert(
    status: str,
    reason: str,
    affected_components: List[str],
    timestamp_str: str,
) -> None:
    """Validate system health outage alert parameters."""
    if not status or not status.strip():
        raise ValueError("Status cannot be blank")
    if not reason or not reason.strip():
        raise ValueError("Reason cannot be blank")
    if not affected_components:
        raise ValueError("Affected components cannot be empty")
    if not timestamp_str or not timestamp_str.strip():
        raise ValueError("Timestamp cannot be blank")


def validate_daily_brief(
    date_str: str,
    market_overview: str,
) -> None:
    """Validate daily brief parameters."""
    if not date_str or not date_str.strip():
        raise ValueError("Date string cannot be blank")
    if not market_overview or not market_overview.strip():
        raise ValueError("Market overview cannot be blank")


def validate_session_summary(
    session_date: str,
    trades_count: int,
    winning_trades: int,
    losing_trades: int,
    gross_pnl: float,
    total_fees: float,
    net_pnl: float,
) -> None:
    """Validate end-of-session summary parameters."""
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
