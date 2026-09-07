"""
Module runner for VeteranDesk background worker.
Allows running via: python -m veterandesk.worker
"""

from __future__ import annotations

import os
import sys
import uvicorn
from veterandesk.api.app import app, broker, ledger, risk_engine
from veterandesk.config import settings
from veterandesk.logging import get_logger
from veterandesk.trading_engine import TradingEngine

logger = get_logger("veterandesk.worker")


def main() -> None:
    engine = TradingEngine(
        ledger=ledger,
        broker=broker,
        risk_engine=risk_engine,
    )

    interval = settings.scrape_interval_seconds
    engine.start_background_loop(interval_seconds=interval)
    logger.info("trading_engine_background_loop_active", interval_seconds=interval)

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(
        "starting_veterandesk_worker_module",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        host=host,
        port=port,
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
