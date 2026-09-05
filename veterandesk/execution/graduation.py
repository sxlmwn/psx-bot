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
from typing import List
from veterandesk.config import settings
from veterandesk.execution.paper_broker import DemoTrade


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


def compute_performance_metrics(
    closed_trades: List[DemoTrade],
    starting_balance: float = 500000.0,
    recent_violations_count: int = 0
) -> PerformanceMetrics:
    """
    Compute official demo performance metrics and determine graduation status.
    """
    total = len(closed_trades)
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

    wins = [t for t in closed_trades if t.net_pnl > 0]
    losses = [t for t in closed_trades if t.net_pnl <= 0]

    num_wins = len(wins)
    num_losses = len(losses)
    win_rate = (num_wins / total) * 100.0

    total_win_amount = sum(t.net_pnl for t in wins)
    total_loss_amount = abs(sum(t.net_pnl for t in losses))

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

    for t in closed_trades:
        running_equity += t.net_pnl
        if running_equity > peak_equity:
            peak_equity = running_equity
        dd = (peak_equity - running_equity) / peak_equity * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    total_net_pnl = sum(t.net_pnl for t in closed_trades)

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
