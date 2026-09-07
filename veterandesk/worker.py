"""
Module runner for VeteranDesk background worker.
Allows running via: python -m veterandesk.worker
"""

from __future__ import annotations

import os
import uvicorn
from veterandesk.api.app import app
from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.worker")


def main() -> None:
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
