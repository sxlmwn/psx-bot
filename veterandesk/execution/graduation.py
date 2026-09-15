"""
Graduation Criteria Module for Demo Account.

Graduation Criteria (Non-negotiable & code-computed):
1. >= 30 closed trades
2. Positive expectancy ((Win Rate * Avg Win) - (Loss Rate * Avg Loss) > 0)
3. Max drawdown < 10.00%
4. Zero rule violations in the last 20 trades
Status cannot be manually edited or overridden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Union
from veterandesk.config import PKT_TZ, settings
from veterandesk.execution.paper_broker import DemoTrade
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.graduation")


@dataclass(frozen=True)
class PerformanceMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    total_net_pnl: float
    avg_win_pkr: float
    avg_loss_pkr: float
    profit_factor: float
    expectancy_pkr: float
    max_drawdown_pct: float
    recent_20_violations_count: int
    is_graduated: bool
    graduation_blockers: List[str]


def _get_trade_field(t: Any, key: str, default: Any = None) -> Any:
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def _get_trade_net_pnl(t: Any) -> float:
    val = _get_trade_field(t, "net_pnl", 0.0)
    return float(val if val is not None else 0.0)


def _get_trade_date(t: Any) -> Optional[date]:
    val = _get_trade_field(t, "opened_at", None)
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(PKT_TZ).date()
        return val.date()
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(PKT_TZ).date()
            return dt.date()
        except Exception:
            return None
    return None


def compute_performance_metrics(
    closed_trades: Sequence[Union[DemoTrade, Dict[str, Any]]],
    starting_balance: float = 500000.0,
    recent_violations_count: int = 0,
    official_start_date: Optional[date] = None,
) -> PerformanceMetrics:
    """
    Compute official demo performance metrics and determine graduation status.
    Excludes invalid trades and trades prior to official_start_date.
    """
    valid_trades: List[Union[DemoTrade, Dict[str, Any]]] = []
    for t in closed_trades:
        is_valid = _get_trade_field(t, "is_valid_signal", True)
        if not is_valid:
            continue
        quality = _get_trade_field(t, "data_quality_flag", "VALID")
        if quality != "VALID":
            continue
        if official_start_date is not None:
            t_date = _get_trade_date(t)
            if t_date is not None and t_date < official_start_date:
                continue
        valid_trades.append(t)

    total = len(valid_trades)
    if total == 0:
        return PerformanceMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate_pct=0.0,
            total_net_pnl=0.0,
            avg_win_pkr=0.0,
            avg_loss_pkr=0.0,
            profit_factor=0.0,
            expectancy_pkr=0.0,
            max_drawdown_pct=0.0,
            recent_20_violations_count=recent_violations_count,
            is_graduated=False,
            graduation_blockers=["Zero closed trades (requires >= 30)"]
        )

    wins = [t for t in valid_trades if _get_trade_net_pnl(t) > 0]
    losses = [t for t in valid_trades if _get_trade_net_pnl(t) <= 0]

    num_wins = len(wins)
    num_losses = len(losses)
    win_rate = (num_wins / total) * 100.0

    total_win_amount = sum(_get_trade_net_pnl(t) for t in wins)
    total_loss_amount = abs(sum(_get_trade_net_pnl(t) for t in losses))

    avg_win = (total_win_amount / num_wins) if num_wins > 0 else 0.0
    avg_loss = (total_loss_amount / num_losses) if num_losses > 0 else 0.0

    profit_factor = (total_win_amount / total_loss_amount) if total_loss_amount > 0 else (999.0 if total_win_amount > 0 else 0.0)

    p_win = num_wins / total
    p_loss = num_losses / total
    expectancy = (p_win * avg_win) - (p_loss * avg_loss)

    # Compute peak-to-trough max drawdown on equity curve
    running_equity = starting_balance
    peak_equity = starting_balance
    max_dd_pct = 0.0

    for t in valid_trades:
        running_equity += _get_trade_net_pnl(t)
        if running_equity > peak_equity:
            peak_equity = running_equity
        dd = (peak_equity - running_equity) / peak_equity * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    total_net_pnl = sum(_get_trade_net_pnl(t) for t in valid_trades)

    # Check graduation criteria
    blockers: List[str] = []
    if total < settings.graduation_min_trades:
        blockers.append(f"Trade count {total} < required {settings.graduation_min_trades}")

    if expectancy <= 0:
        blockers.append(f"Expectancy PKR {expectancy:.2f} is not positive")

    if max_dd_pct >= settings.graduation_max_drawdown_pct:
        blockers.append(f"Max drawdown {max_dd_pct:.2f}% >= maximum allowed {settings.graduation_max_drawdown_pct:.2f}%")

    if recent_violations_count > 0:
        blockers.append(f"{recent_violations_count} rule violations found in recent history (must be 0)")

    is_graduated = len(blockers) == 0

    return PerformanceMetrics(
        total_trades=total,
        winning_trades=num_wins,
        losing_trades=num_losses,
        win_rate_pct=round(win_rate, 2),
        total_net_pnl=round(total_net_pnl, 2),
        avg_win_pkr=round(avg_win, 2),
        avg_loss_pkr=round(avg_loss, 2),
        profit_factor=round(profit_factor, 2),
        expectancy_pkr=round(expectancy, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        recent_20_violations_count=recent_violations_count,
        is_graduated=is_graduated,
        graduation_blockers=blockers
    )


def notify_graduation_status(metrics: PerformanceMetrics) -> bool:
    """Send Telegram and Discord notifications on graduation eligibility change."""
    t_res = False
    d_res = False
    status_str = "GRADUATED" if metrics.is_graduated else "NOT_GRADUATED"
    blockers_str = "All mathematical graduation criteria fulfilled!" if metrics.is_graduated else "; ".join(metrics.graduation_blockers)

    try:
        from veterandesk.alerts.telegram import telegram_service
        t_res = telegram_service.send_graduation_alert(
            status=status_str,
            total_trades=metrics.total_trades,
            win_rate_pct=metrics.win_rate_pct,
            expectancy_pkr=metrics.expectancy_pkr,
            max_drawdown_pct=metrics.max_drawdown_pct,
            blockers_or_status=blockers_str,
        )
    except Exception as ex:
        logger.warning("telegram_graduation_notify_failed", error=str(ex))

    try:
        from veterandesk.alerts.discord import discord_service
        d_res = discord_service.send_graduation_alert(
            status=status_str,
            total_trades=metrics.total_trades,
            win_rate_pct=metrics.win_rate_pct,
            expectancy_pkr=metrics.expectancy_pkr,
            max_drawdown_pct=metrics.max_drawdown_pct,
            blockers_or_status=blockers_str,
        )
    except Exception as ex:
        logger.warning("discord_graduation_notify_failed", error=str(ex))

    return t_res or d_res


