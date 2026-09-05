"""
Individual atomic risk rules for VeteranDesk.
Each rule is an independent, pure, deterministic function
returning RuleResult(passed: bool, reason: str).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
import math
from typing import Any, Optional


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    passed: bool
    reason: str


def check_per_trade_risk(
    account_balance: float,
    entry_price: float,
    stop_loss: float,
    shares: int,
    max_risk_pct: float = 1.00
) -> RuleResult:
    """
    Rule 1: Per-trade risk <= max_risk_pct (default 1.0%).
    Risk = shares * (entry_price - stop_loss).
    Risk % = (Risk / account_balance) * 100.
    """
    if account_balance <= 0:
        return RuleResult(
            rule_name="per_trade_risk",
            passed=False,
            reason=f"Invalid account balance: PKR {account_balance:,.2f}"
        )

    if entry_price <= 0 or stop_loss <= 0:
        return RuleResult(
            rule_name="per_trade_risk",
            passed=False,
            reason=f"Invalid price levels: entry={entry_price}, stop={stop_loss}"
        )

    if stop_loss >= entry_price:
        return RuleResult(
            rule_name="per_trade_risk",
            passed=False,
            reason=f"Stop loss ({stop_loss}) must be strictly below entry ({entry_price})"
        )

    if shares <= 0:
        return RuleResult(
            rule_name="per_trade_risk",
            passed=False,
            reason=f"Shares count ({shares}) must be greater than zero"
        )

    rupee_risk = shares * (entry_price - stop_loss)
    actual_risk_pct = (rupee_risk / account_balance) * 100.0

    # Strict floating point comparison with slight epsilon tolerance for IEEE-754 (1e-6)
    if actual_risk_pct > (max_risk_pct + 1e-6):
        return RuleResult(
            rule_name="per_trade_risk",
            passed=False,
            reason=(
                f"Trade risk {actual_risk_pct:.3f}% exceeds maximum allowed {max_risk_pct:.2f}% "
                f"(Rupee risk: PKR {rupee_risk:,.2f} on PKR {account_balance:,.2f})"
            )
        )

    return RuleResult(
        rule_name="per_trade_risk",
        passed=True,
        reason=f"Risk {actual_risk_pct:.3f}% is within limit {max_risk_pct:.2f}%"
    )


def check_daily_loss_limit(
    current_day_realized_loss: float,
    account_balance: float,
    is_already_halted: bool = False,
    max_daily_loss_pct: float = 2.00
) -> RuleResult:
    """
    Rule 2: Daily loss limit 2% -> halt for the day.
    Halt state survives process restarts and cannot be bypassed.
    """
    if is_already_halted:
        return RuleResult(
            rule_name="daily_loss_limit",
            passed=False,
            reason="Trading is halted for the day due to triggered daily loss limit."
        )

    if account_balance <= 0:
        return RuleResult(
            rule_name="daily_loss_limit",
            passed=False,
            reason=f"Invalid account balance: PKR {account_balance:,.2f}"
        )

    # current_day_realized_loss is positive amount of loss
    daily_loss_pct = (current_day_realized_loss / account_balance) * 100.0

    if daily_loss_pct >= (max_daily_loss_pct - 1e-6):
        return RuleResult(
            rule_name="daily_loss_limit",
            passed=False,
            reason=(
                f"Daily realized loss {daily_loss_pct:.2f}% reached/exceeded daily limit {max_daily_loss_pct:.2f}%. "
                "Immediate halt triggered for the session."
            )
        )

    return RuleResult(
        rule_name="daily_loss_limit",
        passed=True,
        reason=f"Daily loss {daily_loss_pct:.2f}% is below limit {max_daily_loss_pct:.2f}%"
    )


def check_max_intraday_trades(
    trades_executed_today: int,
    max_trades: int = 3
) -> RuleResult:
    """
    Rule 3: Maximum 3 intraday trades per day.
    """
    if trades_executed_today >= max_trades:
        return RuleResult(
            rule_name="max_intraday_trades",
            passed=False,
            reason=f"Daily trade limit reached: {trades_executed_today}/{max_trades} trades executed."
        )

    return RuleResult(
        rule_name="max_intraday_trades",
        passed=True,
        reason=f"Executed trades ({trades_executed_today}) below daily cap ({max_trades})."
    )


def check_entry_time_cutoff(
    current_time_pkt: time,
    cutoff_time_pkt: time = time(15, 0, 0)
) -> RuleResult:
    """
    Rule 4: No new intraday entries after 15:00 PKT.
    """
    if current_time_pkt >= cutoff_time_pkt:
        return RuleResult(
            rule_name="entry_time_cutoff",
            passed=False,
            reason=f"Current time {current_time_pkt.strftime('%H:%M:%S')} PKT is at or after entry cutoff {cutoff_time_pkt.strftime('%H:%M:%S')} PKT."
        )

    return RuleResult(
        rule_name="entry_time_cutoff",
        passed=True,
        reason=f"Current time {current_time_pkt.strftime('%H:%M:%S')} PKT is before cutoff {cutoff_time_pkt.strftime('%H:%M:%S')} PKT."
    )


def check_force_close_time(
    current_time_pkt: time,
    force_close_time_pkt: time = time(15, 20, 0)
) -> RuleResult:
    """
    Rule 5: Force close all intraday positions by 15:20 PKT.
    """
    if current_time_pkt >= force_close_time_pkt:
        return RuleResult(
            rule_name="force_close_time",
            passed=False,
            reason=f"Current time {current_time_pkt.strftime('%H:%M:%S')} PKT reached force close threshold {force_close_time_pkt.strftime('%H:%M:%S')} PKT."
        )

    return RuleResult(
        rule_name="force_close_time",
        passed=True,
        reason="Within intraday open hours before force-close cutoff."
    )


def check_liquidity_cap(
    shares: int,
    twenty_day_adv: float,
    max_adv_pct: float = 5.00
) -> RuleResult:
    """
    Rule 6: Reject if proposed position size > 5% of 20-day Average Daily Volume.
    """
    if twenty_day_adv <= 0:
        return RuleResult(
            rule_name="liquidity_cap",
            passed=False,
            reason=f"Invalid 20-day ADV ({twenty_day_adv}); liquidity cannot be verified."
        )

    position_adv_pct = (shares / twenty_day_adv) * 100.0

    if position_adv_pct > max_adv_pct:
        return RuleResult(
            rule_name="liquidity_cap",
            passed=False,
            reason=f"Position size {shares} is {position_adv_pct:.2f}% of 20-day ADV ({twenty_day_adv:,.0f}), exceeding {max_adv_pct:.1f}% liquidity cap."
        )

    return RuleResult(
        rule_name="liquidity_cap",
        passed=True,
        reason=f"Position size {shares} is {position_adv_pct:.2f}% of 20-day ADV (within {max_adv_pct:.1f}% limit)."
    )


def check_averaging_down(
    ticker: str,
    open_positions: list[dict[str, Any]],
    is_pre_planned: bool = False
) -> RuleResult:
    """
    Rule 7: Averaging down blocked unless pre-planned at entry + thesis intact + within risk cap.
    """
    existing = [p for p in open_positions if p.get("ticker", "").upper() == ticker.upper()]
    if existing and not is_pre_planned:
        return RuleResult(
            rule_name="anti_averaging_down",
            passed=False,
            reason=f"Unplanned averaging down blocked for existing open position in {ticker}."
        )

    return RuleResult(
        rule_name="anti_averaging_down",
        passed=True,
        reason="No unplanned averaging down detected."
    )


def calculate_position_size(
    account_balance: float,
    entry_price: float,
    stop_loss: float,
    risk_pct: float = 1.00,
    lot_size: int = 1,
    cap_by_cash: bool = True,
) -> int:
    """
    Position sizing formula:
    shares = floor((account * risk%) / (entry - stop))
    Rounded down to lot rules (e.g. multiples of lot_size).
    Capped by available cash balance to prevent unbacked margin overdrafts.
    """
    if account_balance <= 0 or entry_price <= stop_loss or risk_pct <= 0:
        return 0

    rupee_budget = account_balance * (risk_pct / 100.0)
    per_share_risk = entry_price - stop_loss

    raw_shares = math.floor(rupee_budget / per_share_risk)

    if cap_by_cash and entry_price > 0:
        # Reserve 1% buffer for execution slippage and PSX transaction fees (commission + regulatory)
        max_cash_shares = math.floor((account_balance * 0.99) / entry_price)
        raw_shares = min(raw_shares, max_cash_shares)

    if lot_size > 1:
        raw_shares = (raw_shares // lot_size) * lot_size

    return max(0, int(raw_shares))
