"""
APScheduler Job Runner for Daily Brief (9:15 AM PKT) and Session Summary (3:45 PM PKT).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from veterandesk.alerts.discord import discord_service
from veterandesk.alerts.telegram import telegram_service
from veterandesk.config import PKT_TZ, settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.scheduler")


def _check_already_sent_today(message_type: str, date_str: str) -> bool:
    """
    Check if a message of the given type was already sent today.
    Uses delivery logs to prevent duplicate sends after process restarts.
    
    Args:
        message_type: "DAILY_BRIEF" or "SESSION_SUMMARY"
        date_str: Date string in YYYY-MM-DD format
        
    Returns:
        True if already sent today, False otherwise
    """
    try:
        from veterandesk.database.session import db_manager
        
        # Map message types to the reference_id format used by telegram/discord services
        # Telegram uses: BRIEF_{date} and SUMMARY_{date}
        # Discord uses: BRIEF_{date} and SUMMARY_{date}
        reference_id_prefix = "BRIEF" if message_type == "DAILY_BRIEF" else "SUMMARY"
        reference_id = f"{reference_id_prefix}_{date_str}"
        
        # Check Telegram delivery log
        client = db_manager.get_client()
        res = client.table("telegram_delivery_log").select("*").eq("message_type", message_type).eq("reference_id", reference_id).execute()
        
        if res.data and any(r.get("status") == "sent" for r in res.data):
            logger.info("message_already_sent_today", message_type=message_type, date=date_str, service="telegram")
            return True
            
        # Check Discord delivery log
        res_discord = client.table("discord_delivery_log").select("*").eq("message_type", message_type).eq("reference_id", reference_id).execute()
        
        if res_discord.data and any(r.get("status") == "sent" for r in res_discord.data):
            logger.info("message_already_sent_today", message_type=message_type, date=date_str, service="discord")
            return True
            
        return False
    except Exception as e:
        logger.warning("failed_to_check_delivery_log", message_type=message_type, date=date_str, error=str(e))
        # Fail-safe: if check fails, assume not sent to avoid blocking legitimate sends
        return False


def run_daily_brief_job(
    date_str: Optional[str] = None,
    watchlist_data: Optional[List[Dict[str, Any]]] = None,
    market_overview: Optional[str] = None,
) -> bool:
    """
    Scheduled 9:15 AM PKT Job: Formats and dispatches pre-market briefing.
    Includes idempotency check to prevent duplicate sends after restarts.
    """
    now_pkt = datetime.now(PKT_TZ)
    today_str = date_str or now_pkt.strftime("%Y-%m-%d")

    # Idempotency check: skip if already sent today
    if _check_already_sent_today("DAILY_BRIEF", today_str):
        logger.info("daily_brief_already_sent_today", date=today_str, skipped=True)
        return True  # Return True since the job effectively "succeeded" (was already sent)

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
    Includes idempotency check to prevent duplicate sends after restarts.
    """
    now_pkt = datetime.now(PKT_TZ)
    date_str = session_date or now_pkt.strftime("%Y-%m-%d")

    # Idempotency check: skip if already sent today
    if _check_already_sent_today("SESSION_SUMMARY", date_str):
        logger.info("session_summary_already_sent_today", date=date_str, skipped=True)
        return True  # Return True since the job effectively "succeeded" (was already sent)

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
    
    Uses persistent SQLAlchemy job store to survive process restarts.
    Jobs configured with 5-minute misfire_grace_time to handle brief restart delays.
    """
    # Configure persistent job store using the same database as the app
    # Convert async database URL to sync URL for SQLAlchemyJobStore (which requires sync engine)
    db_url = settings.database_url
    if "+aiosqlite" in db_url:
        jobstore_url = db_url.replace("+aiosqlite", "")
    elif "+asyncpg" in db_url:
        jobstore_url = db_url.replace("+asyncpg", "")
    else:
        jobstore_url = db_url
    
    jobstores = {
        'default': SQLAlchemyJobStore(url=jobstore_url)
    }
    
    scheduler = BackgroundScheduler(
        timezone=PKT_TZ,
        jobstores=jobstores
    )

    # 1. Daily Brief at 9:15 AM PKT
    # misfire_grace_time=300 (5 minutes) allows brief restart delays without skipping
    scheduler.add_job(
        run_daily_brief_job,
        trigger=CronTrigger(hour=9, minute=15, timezone=PKT_TZ),
        id="daily_brief",
        name="Daily Brief (9:15 AM PKT)",
        replace_existing=True,
        misfire_grace_time=300,  # 5 minutes
    )

    # 2. Session Summary at 3:45 PM PKT (15:45)
    # misfire_grace_time=300 (5 minutes) allows brief restart delays without skipping
    scheduler.add_job(
        run_session_summary_job,
        trigger=CronTrigger(hour=15, minute=45, timezone=PKT_TZ),
        id="session_summary",
        name="Session Summary (3:45 PM PKT)",
        replace_existing=True,
        misfire_grace_time=300,  # 5 minutes
    )

    if start:
        scheduler.start()
        logger.info("alert_scheduler_started", jobstore="sqlalchemy", database_url=jobstore_url)

    return scheduler
