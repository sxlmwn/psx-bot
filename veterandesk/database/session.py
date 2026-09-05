"""
Database Session and Connectivity Manager for VeteranDesk.
Supports PostgreSQL / Supabase and local SQLite fallback.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.database")


class DatabaseManager:
    """
    Manages database connection and live health ping.
    """

    def __init__(self) -> None:
        self.supabase_url: Optional[str] = settings.supabase_url
        self.supabase_key: Optional[str] = settings.supabase_key
        # Check if Supabase URL or PostgreSQL URL is set
        db_url = settings.database_url
        # Clean async driver prefixes for synchronous health check
        if "+aiosqlite" in db_url:
            self.sync_db_url = db_url.replace("+aiosqlite", "")
        elif "+asyncpg" in db_url:
            self.sync_db_url = db_url.replace("+asyncpg", "")
        else:
            self.sync_db_url = db_url

        self._engine: Optional[Engine] = None

    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.sync_db_url, echo=False)
        return self._engine

    def check_connection(self) -> Dict[str, Any]:
        """
        Execute a live health ping against the database.
        Returns detailed status including provider and latency.
        """
        is_supabase = bool(self.supabase_url or "supabase" in self.sync_db_url)
        t0 = time.perf_counter()

        try:
            engine = self.get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            return {
                "status": "GREEN",
                "provider": "Supabase (PostgreSQL)" if is_supabase else "Local SQLite Fallback",
                "supabase_configured": is_supabase,
                "latency_ms": latency_ms,
                "message": (
                    "Connected to live Supabase PostgreSQL" if is_supabase
                    else "Connected to local SQLite database (SUPABASE_URL not configured in .env)"
                )
            }
        except Exception as e:
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            logger.error("database_health_check_failed", error=str(e), is_supabase=is_supabase)
            return {
                "status": "RED",
                "provider": "Supabase (PostgreSQL)" if is_supabase else "Local SQLite Fallback",
                "supabase_configured": is_supabase,
                "latency_ms": latency_ms,
                "message": f"Connection failed: {str(e)}"
            }


db_manager = DatabaseManager()
