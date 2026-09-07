#!/usr/bin/env python3
"""
VeteranDesk Background Trading Engine - Production Worker Entrypoint.

Starts:
1. Background Trading Engine (every 30s during market hours):
   - Scrapes PSX DPS live timeseries for all watchlist tickers
   - Constructs chronological 1-minute OHLCV candles
   - Evaluates Opening Range Breakout (ORB v1.0) strategy
   - Enforces strict Risk & Discipline Engine rules (1% max risk, 2% daily loss, 15:00 cutoff)
   - Executes paper trades via Double-Entry Ledger
   - Dispatches real-time Telegram & Discord alerts
   - Monitors open position exit criteria (Target Hit, Stop Hit, 15:20 PKT force close)
   - Emits structured, transparent logs for every cycle and ticker check

2. APScheduler:
   - Daily Brief at 09:15 PKT
   - Session Summary at 15:45 PKT
   - Continuous 60s Subsystem Health Heartbeats persisted to Supabase

3. Embedded FastAPI HTTP Server on $PORT (default 8000):
   - GET /       -> Root health probe
   - GET /health -> Detailed component status & database latency
   - Trade execution & metrics REST endpoints
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from veterandesk.api.app import app, broker, ledger, risk_engine
from veterandesk.config import settings
from veterandesk.logging import get_logger
from veterandesk.trading_engine import TradingEngine, is_psx_market_open

logger = get_logger("veterandesk.worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="VeteranDesk Background Trading Engine Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single trading cycle immediately and exit (useful for testing and diagnostics)",
    )
    parser.add_argument(
        "--force-scan",
        action="store_true",
        help="Force market data scan even if outside PSX trading hours",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Run trading loop without starting the embedded FastAPI HTTP server",
    )
    args = parser.parse_args()

    engine = TradingEngine(
        ledger=ledger,
        broker=broker,
        risk_engine=risk_engine,
    )

    if args.once:
        logger.info("running_single_trading_cycle_cli", force_scan=args.force_scan or True)
        result = engine.run_trading_cycle(force_scan=True)
        print("\n" + "=" * 80)
        print("VETERANDESK TRADING CYCLE SUMMARY")
        print("=" * 80)
        for k, v in result.items():
            print(f"  {k}: {v}")
        print("=" * 80)
        sys.exit(0)

    # Start Trading Engine in background thread
    interval = settings.scrape_interval_seconds
    engine.start_background_loop(interval_seconds=interval)
    logger.info("trading_engine_background_loop_active", interval_seconds=interval)

    if args.no_server:
        logger.info("running_in_pure_worker_mode_no_http_server")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            engine.stop_background_loop()
            logger.info("worker_stopped_by_user")
            sys.exit(0)

    # Default mode: run embedded FastAPI server on $PORT for Railway/Koyeb health probes
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(
        "starting_veterandesk_worker_service",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        host=host,
        port=port,
        scrape_interval_seconds=interval,
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
