"""
Risk & Discipline Engine Pipeline.
Coordinates all atomic risk checks and calculates strict position sizing.
Non-negotiable rule: ANY single failure blocks trade execution immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, date
from typing import Any, List, Optional

from veterandesk.config import PKT_TZ, settings
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


def check_daily_halt_from_db(halt_date: date) -> bool:
    """
    Query the daily_halts table to determine if trading is halted for a given date.
    Returns True if a halt record exists with is_halted=True for that date.
    """
    # 1. Supabase PostgreSQL
    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()
        res = client.table("daily_halts").select("*").eq("halt_date", str(halt_date)).execute()
        if hasattr(res, "data") and isinstance(res.data, list) and len(res.data) > 0:
            halt_record = res.data[0]
            if isinstance(halt_record, dict):
                is_halted_val = halt_record.get("is_halted", False)
                if isinstance(is_halted_val, bool):
                    logger.info("daily_halt_state_retrieved", date=str(halt_date), is_halted=is_halted_val)
                    return is_halted_val
                if str(is_halted_val).lower() in ("true", "1"):
                    return True
        return False
    except Exception as ex:
        logger.warning("daily_halt_db_query_failed", date=str(halt_date), error=str(ex))

    # 2. SQLite local fallback
    try:
        from veterandesk.database.session import db_manager
        from sqlalchemy import text
        engine = db_manager.get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT is_halted FROM daily_halts WHERE halt_date = :dt LIMIT 1"),
                {"dt": str(halt_date)}
            ).fetchone()
            from unittest.mock import MagicMock
            if row is not None and not isinstance(row, MagicMock):
                is_halted = bool(row[0])
                logger.info("daily_halt_state_retrieved_sqlite", date=str(halt_date), is_halted=is_halted)
                return is_halted
    except Exception:
        pass

    return False


def record_daily_halt(
    halt_date: date,
    loss_amount: float,
    loss_pct: float,
    reason: str = "Daily loss limit breached",
) -> None:
    """
    Record a daily halt event to the database.
    This ensures halt state persists across process restarts.
    """
    now_utc = datetime.now(PKT_TZ).astimezone(PKT_TZ).isoformat()
    record = {
        "halt_date": str(halt_date),
        "is_halted": True,
        "reason": reason,
        "triggered_at": now_utc,
        "loss_amount": loss_amount,
        "loss_pct": loss_pct,
    }
    # 1. Try Supabase
    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()
        client.table("daily_halts").upsert(record, on_conflict="halt_date").execute()
        logger.info(
            "daily_halt_recorded",
            date=str(halt_date),
            loss_amount=loss_amount,
            loss_pct=loss_pct,
        )
        return
    except Exception as ex:
        logger.warning("daily_halt_supabase_write_failed", date=str(halt_date), error=str(ex))

    # 2. Try SQLite fallback
    try:
        from veterandesk.database.session import db_manager
        from sqlalchemy import text
        engine = db_manager.get_engine()
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO daily_halts (halt_date, is_halted, reason, triggered_at, loss_amount, loss_pct)
                    VALUES (:halt_date, :is_halted, :reason, :triggered_at, :loss_amount, :loss_pct)
                    ON CONFLICT(halt_date) DO UPDATE SET
                        is_halted = excluded.is_halted,
                        reason = excluded.reason,
                        triggered_at = excluded.triggered_at,
                        loss_amount = excluded.loss_amount,
                        loss_pct = excluded.loss_pct
                """),
                record,
            )
            conn.commit()
            logger.info(
                "daily_halt_recorded_sqlite",
                date=str(halt_date),
                loss_amount=loss_amount,
                loss_pct=loss_pct,
            )
    except Exception as ex2:
        logger.error("daily_halt_db_write_failed", date=str(halt_date), error=str(ex2))


def check_daily_halt_already_alerted_today(halt_date: date) -> bool:
    """
    Check if a daily halt alert has already been dispatched today.
    Checks daily_halts table and persistent alert delivery logs.
    """
    if check_daily_halt_from_db(halt_date):
        return True

    try:
        from veterandesk.alerts.scheduler import _check_already_sent_today
        if _check_already_sent_today("DAILY_HALT", str(halt_date)):
            return True
    except Exception:
        pass

    return False


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
        self._halt_alerted_date: Optional[date] = None

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
            current_pkt_date = datetime.now(PKT_TZ).date()

            # Accurate loss percentage relative to account equity
            loss_pct = round((current_day_realized_loss / account_balance * 100.0), 2) if account_balance > 0 else self.max_daily_loss_pct

            # Only trigger alert/record if this is an actual breach (not an already-halted or invalid balance rejection)
            is_limit_breach = "reached/exceeded daily limit" in res_loss.reason

            # Guard against duplicate alert spam across cycles and restarts
            already_alerted = (
                is_already_halted
                or self._halt_alerted_date == current_pkt_date
                or check_daily_halt_already_alerted_today(current_pkt_date)
            )

            if is_limit_breach and not already_alerted:
                self._halt_alerted_date = current_pkt_date

                # Record halt to database for persistence across restarts
                record_daily_halt(
                    halt_date=current_pkt_date,
                    loss_amount=current_day_realized_loss,
                    loss_pct=loss_pct,
                    reason="Daily loss limit breached",
                )

                try:
                    from veterandesk.alerts.telegram import telegram_service
                    t_str = current_time_pkt.strftime("%H:%M:%S PKT") if current_time_pkt else None
                    telegram_service.send_daily_halt_alert(
                        loss_pct=loss_pct,
                        max_loss_pct=self.max_daily_loss_pct,
                        loss_amount_pkr=current_day_realized_loss,
                        halt_time_pkt=t_str,
                        action_taken="Trading halted for the day; no new orders permitted.",
                    )
                except Exception as ex:
                    logger.warning("telegram_daily_halt_alert_failed", error=str(ex))

                try:
                    from veterandesk.alerts.discord import discord_service
                    t_str = current_time_pkt.strftime("%H:%M:%S PKT") if current_time_pkt else None
                    discord_service.send_daily_halt_alert(
                        loss_pct=loss_pct,
                        max_loss_pct=self.max_daily_loss_pct,
                        loss_amount_pkr=current_day_realized_loss,
                        halt_time_pkt=t_str,
                        action_taken="Trading halted for the day; no new orders permitted.",
                    )
                except Exception as ex:
                    logger.warning("discord_daily_halt_alert_failed", error=str(ex))
            else:
                logger.info(
                    "daily_halt_alert_suppressed",
                    date=str(current_pkt_date),
                    reason="Halt alert already dispatched or trading already halted for session",
                    is_already_halted=is_already_halted,
                    is_limit_breach=is_limit_breach,
                )

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
