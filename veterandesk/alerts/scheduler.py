"""
APScheduler Job Runner for Daily Brief (9:15 AM PKT) and Session Summary (3:45 PM PKT).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from veterandesk.alerts.discord import discord_service
from veterandesk.alerts.telegram import telegram_service
from veterandesk.config import PKT_TZ, settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.scheduler")


def run_daily_brief_job(
    date_str: Optional[str] = None,
    watchlist_data: Optional[List[Dict[str, Any]]] = None,
    market_overview: Optional[str] = None,
) -> bool:
    """
    Scheduled 9:15 AM PKT Job: Formats and dispatches pre-market briefing.
    """
    now_pkt = datetime.now(PKT_TZ)
    today_str = date_str or now_pkt.strftime("%Y-%m-%d")

    # Build default watchlist snapshot if none provided
    if not watchlist_data:
        watchlist_data = [
            {"ticker": sym, "price": 0.0, "change_pct": 0.0}
            for sym in settings.watchlist[:8]
        ]

    overview = market_overview or (
        "PSX KSE-100 opening session. ORB breakout strategy active across liquid symbols. "
        "Strict 1% risk per trade ceiling and 15:00 PKT entry cutoff enforced."
    )
    key_levels = [
        "KSE-100 Range: Monitoring opening 15-min price action",
        "Volume filter: 1.5x minimum expansion required for breakout validity",
    ]

    t_ok = False
    d_ok = False

    try:
        t_ok = telegram_service.send_daily_brief(
            date_str=today_str,
            market_overview=overview,
            watchlist_summary=watchlist_data,
            key_levels=key_levels,
        )
        logger.info("telegram_daily_brief_job_dispatched", date=today_str, success=t_ok)
    except Exception as e:
        logger.error("telegram_daily_brief_job_failed", error=str(e), date=today_str)

    try:
        d_ok = discord_service.send_daily_brief(
            date_str=today_str,
            market_overview=overview,
            watchlist_summary=watchlist_data,
            key_levels=key_levels,
        )
        logger.info("discord_daily_brief_job_dispatched", date=today_str, success=d_ok)
    except Exception as e:
        logger.error("discord_daily_brief_job_failed", error=str(e), date=today_str)

    return t_ok or d_ok


def run_session_summary_job(
    session_date: Optional[str] = None,
    trades_count: int = 0,
    winning_trades: int = 0,
    losing_trades: int = 0,
    gross_pnl: float = 0.0,
    total_fees: float = 0.0,
    net_pnl: float = 0.0,
    discipline_violations: int = 0,
    ending_cash: float = 500000.0,
) -> bool:
    """
    Scheduled 3:45 PM PKT Job: Formats and dispatches post-market session summary.
    """
    now_pkt = datetime.now(PKT_TZ)
    date_str = session_date or now_pkt.strftime("%Y-%m-%d")

    t_ok = False
    d_ok = False

    try:
        t_ok = telegram_service.send_session_summary(
            session_date=date_str,
            trades_count=trades_count,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            discipline_violations=discipline_violations,
            ending_cash=ending_cash,
        )
        logger.info("telegram_session_summary_job_dispatched", date=date_str, success=t_ok)
    except Exception as e:
        logger.error("telegram_session_summary_job_failed", error=str(e), date=date_str)

    try:
        d_ok = discord_service.send_session_summary(
            session_date=date_str,
            trades_count=trades_count,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            discipline_violations=discipline_violations,
            ending_cash=ending_cash,
        )
        logger.info("discord_session_summary_job_dispatched", date=date_str, success=d_ok)
    except Exception as e:
        logger.error("discord_session_summary_job_failed", error=str(e), date=date_str)

    return t_ok or d_ok


def create_alert_scheduler(
    start: bool = False,
) -> BackgroundScheduler:
    """
    Configure APScheduler with:
    1. Daily Brief: 9:15 AM PKT
    2. Session Summary: 3:45 PM PKT
    """
    scheduler = BackgroundScheduler(timezone=PKT_TZ)

    # 1. Daily Brief at 9:15 AM PKT
    scheduler.add_job(
        run_daily_brief_job,
        trigger=CronTrigger(hour=9, minute=15, timezone=PKT_TZ),
        id="telegram_daily_brief",
        name="Telegram Daily Brief (9:15 AM PKT)",
        replace_existing=True,
    )

    # 2. Session Summary at 3:45 PM PKT (15:45)
    scheduler.add_job(
        run_session_summary_job,
        trigger=CronTrigger(hour=15, minute=45, timezone=PKT_TZ),
        id="telegram_session_summary",
        name="Telegram Session Summary (3:45 PM PKT)",
        replace_existing=True,
    )

    if start:
        scheduler.start()
        logger.info("alert_scheduler_started")

    return scheduler
