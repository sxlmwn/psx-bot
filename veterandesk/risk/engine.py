"""
Risk & Discipline Engine Pipeline.
Coordinates all atomic risk checks and calculates strict position sizing.
Non-negotiable rule: ANY single failure blocks trade execution immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, List, Optional

from veterandesk.config import settings
from veterandesk.logging import get_logger
from veterandesk.risk.rules import (
    RuleResult,
    calculate_position_size,
    check_averaging_down,
    check_daily_loss_limit,
    check_entry_time_cutoff,
    check_liquidity_cap,
    check_max_intraday_trades,
    check_per_trade_risk,
)
from veterandesk.strategy.models import TradeSignal, SignalStatus

logger = get_logger("veterandesk.risk_engine")


@dataclass(frozen=True)
class RiskAssessment:
    is_approved: bool
    approved_shares: int
    rule_results: List[RuleResult]
    rejection_reasons: List[str]
    risk_pct_used: float


class RiskEngine:
    """
    Central Risk and Discipline Gatekeeper.
    Every trade MUST pass through this engine prior to execution.
    """

    def __init__(
        self,
        max_risk_per_trade_pct: float = 1.00,
        max_daily_loss_pct: float = 2.00,
        max_intraday_trades: int = 3,
        entry_cutoff_pkt: time = time(15, 0, 0),
        force_close_pkt: time = time(15, 20, 0),
        max_adv_pct: float = 5.00,
        lot_size: int = 1,
    ) -> None:
        # Enforce hard ceiling in constructor
        if max_risk_per_trade_pct > 1.00:
            raise ValueError("Configuration breach: max_risk_per_trade_pct cannot exceed 1.00%")
        if max_daily_loss_pct > 5.00:
            raise ValueError("Configuration breach: max_daily_loss_pct cannot exceed 5.00%")

        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_intraday_trades = max_intraday_trades
        self.entry_cutoff_pkt = entry_cutoff_pkt
        self.force_close_pkt = force_close_pkt
        self.max_adv_pct = max_adv_pct
        self.lot_size = lot_size

    def evaluate_signal(
        self,
        signal: TradeSignal,
        account_balance: float,
        current_day_realized_loss: float,
        trades_executed_today: int,
        current_time_pkt: time,
        twenty_day_adv: float,
        open_positions: list[dict[str, Any]],
        is_already_halted: bool = False,
        is_pre_planned_add: bool = False,
    ) -> RiskAssessment:
        """
        Evaluate signal against all non-negotiable risk rules.
        """
        results: List[RuleResult] = []

        # 1. Daily Loss Limit Check
        res_loss = check_daily_loss_limit(
            current_day_realized_loss=current_day_realized_loss,
            account_balance=account_balance,
            is_already_halted=is_already_halted,
            max_daily_loss_pct=self.max_daily_loss_pct,
        )
        results.append(res_loss)
        if not res_loss.passed:
            try:
                from veterandesk.alerts.telegram import telegram_service
                loss_pct = (current_day_realized_loss / account_balance * 100.0) if account_balance > 0 else self.max_daily_loss_pct
                t_str = current_time_pkt.strftime("%H:%M:%S PKT") if current_time_pkt else None
                telegram_service.send_daily_halt_alert(
                    loss_pct=max(loss_pct, self.max_daily_loss_pct),
                    max_loss_pct=self.max_daily_loss_pct,
                    loss_amount_pkr=current_day_realized_loss if current_day_realized_loss > 0 else 10000.0,
                    halt_time_pkt=t_str,
                    action_taken="Trading halted for the day; no new orders permitted.",
                )
            except Exception as ex:
                logger.warning("telegram_daily_halt_alert_failed", error=str(ex))

            try:
                from veterandesk.alerts.discord import discord_service
                loss_pct = (current_day_realized_loss / account_balance * 100.0) if account_balance > 0 else self.max_daily_loss_pct
                t_str = current_time_pkt.strftime("%H:%M:%S PKT") if current_time_pkt else None
                discord_service.send_daily_halt_alert(
                    loss_pct=max(loss_pct, self.max_daily_loss_pct),
                    max_loss_pct=self.max_daily_loss_pct,
                    loss_amount_pkr=current_day_realized_loss if current_day_realized_loss > 0 else 10000.0,
                    halt_time_pkt=t_str,
                    action_taken="Trading halted for the day; no new orders permitted.",
                )
            except Exception as ex:
                logger.warning("discord_daily_halt_alert_failed", error=str(ex))

        # 2. Daily Trade Count Check
        res_trades = check_max_intraday_trades(
            trades_executed_today=trades_executed_today,
            max_trades=self.max_intraday_trades,
        )
        results.append(res_trades)

        # 3. Entry Time Cutoff Check (15:00 PKT)
        res_time = check_entry_time_cutoff(
            current_time_pkt=current_time_pkt,
            cutoff_time_pkt=self.entry_cutoff_pkt,
        )
        results.append(res_time)

        # 4. Anti-Averaging Down Check
        res_avg = check_averaging_down(
            ticker=signal.ticker,
            open_positions=open_positions,
            is_pre_planned=is_pre_planned_add,
        )
        results.append(res_avg)

        # 5. Position Sizing
        shares = calculate_position_size(
            account_balance=account_balance,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            risk_pct=self.max_risk_per_trade_pct,
            lot_size=self.lot_size,
        )

        if shares <= 0:
            results.append(
                RuleResult(
                    rule_name="position_sizing",
                    passed=False,
                    reason=f"Position size computed to 0 shares (Balance: PKR {account_balance:,.2f}, Risk: {signal.entry_price - signal.stop_loss:.2f})",
                )
            )
        else:
            # 6. Per-Trade Risk Verification
            res_risk = check_per_trade_risk(
                account_balance=account_balance,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                shares=shares,
                max_risk_pct=self.max_risk_per_trade_pct,
            )
            results.append(res_risk)

            # 7. Liquidity ADV Cap Check
            res_liq = check_liquidity_cap(
                shares=shares,
                twenty_day_adv=twenty_day_adv,
                max_adv_pct=self.max_adv_pct,
            )
            results.append(res_liq)

        # Evaluation outcome
        rejection_reasons = [r.reason for r in results if not r.passed]
        is_approved = len(rejection_reasons) == 0

        risk_used = 0.0
        if is_approved and shares > 0 and account_balance > 0:
            rupee_risk = shares * (signal.entry_price - signal.stop_loss)
            risk_used = round((rupee_risk / account_balance) * 100.0, 3)

        logger.info(
            "risk_evaluation_complete",
            ticker=signal.ticker,
            is_approved=is_approved,
            approved_shares=shares if is_approved else 0,
            failed_rules_count=len(rejection_reasons),
            risk_pct_used=risk_used,
        )

        return RiskAssessment(
            is_approved=is_approved,
            approved_shares=shares if is_approved else 0,
            rule_results=results,
            rejection_reasons=rejection_reasons,
            risk_pct_used=risk_used,
        )


# Global singleton instance
risk_engine = RiskEngine(
    max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
    max_daily_loss_pct=settings.max_daily_loss_pct,
    max_intraday_trades=settings.max_intraday_trades_per_day,
    entry_cutoff_pkt=settings.entry_cutoff_pkt,
    force_close_pkt=settings.force_close_pkt,
    max_adv_pct=settings.max_adv_percentage,
)
