#!/usr/bin/env python3
"""
VeteranDesk Background Trading Engine - Koyeb Worker Entrypoint.

Starts:
1. APScheduler:
   - Daily Brief (09:15 PKT)
   - Session Summary (15:45 PKT)
   - Continuous 60s Subsystem Health Heartbeat to Supabase
2. Embedded FastAPI HTTP Server on $PORT (default 8000):
   - GET /       -> Root health probe
   - GET /health -> Detailed component status & database latency
   - Trade execution & metrics endpoints
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from veterandesk.api.app import app
from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.worker")


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(
        "starting_veterandesk_worker",
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
