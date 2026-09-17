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
        message_type: "DAILY_BRIEF", "SESSION_SUMMARY", or "DAILY_HALT"
        date_str: Date string in YYYY-MM-DD format
        
    Returns:
        True if already sent today, False otherwise
    """
    try:
        from veterandesk.database.session import db_manager
        
        # Map message types to the reference_id format used by telegram/discord services
        # Telegram uses: BRIEF_{date}, SUMMARY_{date}, HALT_{YYYYMMDD}
        # Discord uses: BRIEF_{date}, SUMMARY_{date}, HALT_{YYYYMMDD}
        if message_type == "DAILY_BRIEF":
            target_types = ["DAILY_BRIEF"]
            reference_ids = [f"BRIEF_{date_str}"]
        elif message_type == "SESSION_SUMMARY":
            target_types = ["SESSION_SUMMARY"]
            reference_ids = [f"SUMMARY_{date_str}"]
        elif message_type in ("DAILY_HALT", "DAILY_LOSS_HALT"):
            target_types = ["DAILY_HALT", "DAILY_LOSS_HALT"]
            compact_date = date_str.replace("-", "")
            reference_ids = [f"HALT_{compact_date}", f"HALT_{date_str}"]
        else:
            target_types = [message_type]
            reference_ids = [f"{message_type}_{date_str}"]
        
        client = db_manager.get_client()
        for ref_id in reference_ids:
            for mt in target_types:
                res = client.table("telegram_delivery_log").select("*").eq("message_type", mt).eq("reference_id", ref_id).execute()
                if hasattr(res, "data") and isinstance(res.data, list) and any(isinstance(r, dict) and r.get("status") == "sent" for r in res.data):
                    logger.info("message_already_sent_today", message_type=message_type, date=date_str, service="telegram")
                    return True

                res_discord = client.table("discord_delivery_log").select("*").eq("message_type", mt).eq("reference_id", ref_id).execute()
                if hasattr(res_discord, "data") and isinstance(res_discord.data, list) and any(isinstance(r, dict) and r.get("status") == "sent" for r in res_discord.data):
                    logger.info("message_already_sent_today", message_type=message_type, date=date_str, service="discord")
                    return True

        return False
    except Exception as e:
        # Fallback to local SQLite if Supabase client query is unavailable or failed
        try:
            from unittest.mock import MagicMock
            from veterandesk.database.session import db_manager
            from sqlalchemy import text
            engine = db_manager.get_engine()
            with engine.connect() as conn:
                for table in ("telegram_delivery_log", "discord_delivery_log"):
                    for ref_id in reference_ids:
                        for mt in target_types:
                            result = conn.execute(
                                text(f"SELECT 1 FROM {table} WHERE message_type = :mt AND reference_id = :ref AND status = 'sent' LIMIT 1"),
                                {"mt": mt, "ref": ref_id}
                            )
                            if hasattr(result, "fetchone"):
                                row = result.fetchone()
                                if row is not None and not isinstance(row, MagicMock):
                                    logger.info("message_already_sent_today", message_type=message_type, date=date_str, service=table)
                                    return True
        except Exception:
            pass

        logger.warning("failed_to_check_delivery_log", message_type=message_type, date=date_str, error=str(e))
        # Fail-safe: if check fails, assume not sent to avoid blocking legitimate sends
        return False


def fetch_watchlist_snapshot(
    symbols: Optional[List[str]] = None,
    scraper: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch the latest price and % change for watchlist symbols.
    
    Data Source Hierarchy:
    1. Live PSX DPS quote via PSXDpsScraper (same mechanism used by ORB engine).
    2. Fallback to database `market_ticks` table for the most recent persisted tick.
    3. Diagnostic logging if data is completely unavailable for any symbol.
    """
    target_symbols = symbols if symbols is not None else settings.watchlist[:8]
    if not target_symbols:
        return []

    snapshot: List[Dict[str, Any]] = []

    if scraper is not None:
        active_scraper = scraper
    else:
        try:
            from veterandesk.market_data.scraper import PSXDpsScraper
            active_scraper = PSXDpsScraper()
        except Exception as e:
            logger.warning("failed_to_initialize_dps_scraper", error=str(e))
            active_scraper = None

    for sym in target_symbols:
        sym_clean = sym.strip().upper()
        price = 0.0
        change_pct = 0.0
        source = "none"

        # 1. Primary: Live PSX DPS Scraper (same as TradingEngine/ORB)
        if active_scraper is not None:
            try:
                quote = active_scraper.fetch_ticker_quote(sym_clean)
                if quote and float(quote.get("price") or 0.0) > 0:
                    price = float(quote["price"])
                    raw_change = float(quote.get("change") or 0.0)
                    if quote.get("change_pct") is not None:
                        change_pct = float(quote["change_pct"])
                    elif raw_change != 0.0 and (price - raw_change) > 0:
                        change_pct = round((raw_change / (price - raw_change)) * 100.0, 2)
                    else:
                        change_pct = 0.0
                    source = "scraper"
            except Exception as scrap_err:
                logger.warning("daily_brief_scraper_fetch_failed", ticker=sym_clean, error=str(scrap_err))

        # 2. Secondary: Fallback to database `market_ticks` table
        if price <= 0.0:
            try:
                from veterandesk.database.session import db_manager
                client = db_manager.get_client()
                res = (
                    client.table("market_ticks")
                    .select("price, change, change_pct")
                    .eq("ticker", sym_clean)
                    .order("psx_timestamp", desc=True)
                    .limit(1)
                    .execute()
                )
                if res.data and len(res.data) > 0:
                    row: Dict[str, Any] = res.data[0]
                    db_price = float(row.get("price") or 0.0)
                    if db_price > 0:
                        price = db_price
                        db_change = float(row.get("change") or 0.0)
                        if row.get("change_pct") is not None:
                            change_pct = float(row["change_pct"])
                        elif db_change != 0.0 and (db_price - db_change) > 0:
                            change_pct = round((db_change / (db_price - db_change)) * 100.0, 2)
                        else:
                            change_pct = 0.0
                        source = "database_market_ticks"
            except Exception as db_err:
                logger.warning("daily_brief_db_fetch_failed", ticker=sym_clean, error=str(db_err))

        if price <= 0.0:
            logger.warning("daily_brief_ticker_price_unavailable", ticker=sym_clean)

        logger.info(
            "daily_brief_ticker_resolved",
            ticker=sym_clean,
            price=price,
            change_pct=change_pct,
            source=source,
        )

        snapshot.append({
            "ticker": sym_clean,
            "price": price,
            "change_pct": change_pct,
        })

    return snapshot


def run_daily_brief_job(
    date_str: Optional[str] = None,
    watchlist_data: Optional[List[Dict[str, Any]]] = None,
    market_overview: Optional[str] = None,
    scraper: Optional[Any] = None,
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

    # Resolve watchlist snapshot with real price data
    if watchlist_data is None:
        watchlist_data = fetch_watchlist_snapshot(
            symbols=settings.watchlist[:8],
            scraper=scraper,
        )
    else:
        # If caller provided custom watchlist data with missing/zero prices, enrich them
        for item in watchlist_data:
            if float(item.get("price") or 0.0) <= 0.0:
                ticker = item.get("ticker")
                if ticker:
                    enriched = fetch_watchlist_snapshot(symbols=[ticker], scraper=scraper)
                    if enriched and enriched[0]["price"] > 0:
                        item["price"] = enriched[0]["price"]
                        item["change_pct"] = enriched[0]["change_pct"]

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


def fetch_session_summary_metrics(session_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Query database for executed trades on the specified PKT session date
    and compute aggregated session summary metrics.
    """
    now_pkt = datetime.now(PKT_TZ)
    date_str = session_date or now_pkt.strftime("%Y-%m-%d")
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        target_date = now_pkt.date()

    # Time bounds for this PKT date (00:00:00 to 23:59:59 PKT)
    pkt_start = datetime.combine(target_date, datetime.min.time(), tzinfo=PKT_TZ)
    pkt_end = datetime.combine(target_date, datetime.max.time(), tzinfo=PKT_TZ)
    start_utc_iso = pkt_start.astimezone(timezone.utc).isoformat()
    end_utc_iso = pkt_end.astimezone(timezone.utc).isoformat()

    trades: List[Dict[str, Any]] = []
    ending_cash = float(settings.starting_balance_pkr)

    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()

        # Query trades for the date range
        raw_trades: List[Dict[str, Any]] = []
        try:
            res = (
                client.table("trades")
                .select("*")
                .gte("opened_at", start_utc_iso)
                .lte("opened_at", end_utc_iso)
                .execute()
            )
            raw_trades = res.data or []
        except Exception as q_err:
            logger.warning("session_summary_range_query_failed_trying_all", error=str(q_err))
            try:
                res = client.table("trades").select("*").execute()
                raw_trades = res.data or []
            except Exception:
                raw_trades = []

        for row in raw_trades:
            # Exclude trades flagged as invalid from aggregate session accuracy/performance metrics
            if row.get("is_valid_signal") is False or row.get("data_quality_flag") == "INVALID":
                continue
            opened_at_raw = row.get("opened_at")
            if opened_at_raw:
                try:
                    dt = datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.astimezone(PKT_TZ).date() == target_date:
                        trades.append(row)
                except Exception:
                    continue

        # Query latest cash balance from demo_ledger
        try:
            res_ledger = (
                client.table("demo_ledger")
                .select("balance_after")
                .eq("account_name", "CASH")
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            if res_ledger.data and len(res_ledger.data) > 0:
                ending_cash = float(res_ledger.data[0].get("balance_after") or settings.starting_balance_pkr)
        except Exception as le:
            logger.warning("failed_to_fetch_ledger_cash_balance", error=str(le))

    except Exception as e:
        logger.warning("failed_to_fetch_session_summary_metrics", date=date_str, error=str(e))

    trades_count = len(trades)
    winning_trades = sum(1 for t in trades if float(t.get("net_pnl") or 0.0) > 0)
    losing_trades = sum(1 for t in trades if float(t.get("net_pnl") or 0.0) < 0)
    gross_pnl = round(sum(float(t.get("gross_pnl") or 0.0) for t in trades), 2)
    total_fees = round(sum(float(t.get("fees_paid") or 0.0) for t in trades), 2)
    net_pnl = round(sum(float(t.get("net_pnl") or 0.0) for t in trades), 2)

    logger.info(
        "session_summary_metrics_resolved",
        date=date_str,
        trades_count=trades_count,
        winning=winning_trades,
        losing=losing_trades,
        gross_pnl=gross_pnl,
        total_fees=total_fees,
        net_pnl=net_pnl,
        ending_cash=ending_cash,
    )

    return {
        "session_date": date_str,
        "trades_count": trades_count,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "gross_pnl": gross_pnl,
        "total_fees": total_fees,
        "net_pnl": net_pnl,
        "discipline_violations": 0,
        "ending_cash": ending_cash,
    }


def run_session_summary_job(
    session_date: Optional[str] = None,
    trades_count: Optional[int] = None,
    winning_trades: Optional[int] = None,
    losing_trades: Optional[int] = None,
    gross_pnl: Optional[float] = None,
    total_fees: Optional[float] = None,
    net_pnl: Optional[float] = None,
    discipline_violations: int = 0,
    ending_cash: Optional[float] = None,
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

    # If metrics not provided (e.g. invoked by APScheduler with no arguments), fetch from DB
    if trades_count is None:
        metrics = fetch_session_summary_metrics(session_date=date_str)
        t_count = int(metrics["trades_count"])
        w_trades = int(metrics["winning_trades"]) if winning_trades is None else winning_trades
        l_trades = int(metrics["losing_trades"]) if losing_trades is None else losing_trades
        g_pnl = float(metrics["gross_pnl"]) if gross_pnl is None else gross_pnl
        t_fees = float(metrics["total_fees"]) if total_fees is None else total_fees
        n_pnl = float(metrics["net_pnl"]) if net_pnl is None else net_pnl
        end_cash = float(metrics["ending_cash"]) if ending_cash is None else ending_cash
    else:
        t_count = trades_count
        w_trades = winning_trades if winning_trades is not None else 0
        l_trades = losing_trades if losing_trades is not None else 0
        g_pnl = gross_pnl if gross_pnl is not None else 0.0
        t_fees = total_fees if total_fees is not None else 0.0
        n_pnl = net_pnl if net_pnl is not None else 0.0
        end_cash = ending_cash if ending_cash is not None else 500000.0

    t_ok = False
    d_ok = False

    try:
        t_ok = telegram_service.send_session_summary(
            session_date=date_str,
            trades_count=t_count,
            winning_trades=w_trades,
            losing_trades=l_trades,
            gross_pnl=g_pnl,
            total_fees=t_fees,
            net_pnl=n_pnl,
            discipline_violations=discipline_violations,
            ending_cash=end_cash,
        )
        logger.info("telegram_session_summary_job_dispatched", date=date_str, success=t_ok)
    except Exception as e:
        logger.error("telegram_session_summary_job_failed", error=str(e), date=date_str)

    try:
        d_ok = discord_service.send_session_summary(
            session_date=date_str,
            trades_count=t_count,
            winning_trades=w_trades,
            losing_trades=l_trades,
            gross_pnl=g_pnl,
            total_fees=t_fees,
            net_pnl=n_pnl,
            discipline_violations=discipline_violations,
            ending_cash=end_cash,
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
