"""
Database Session and Connectivity Manager for VeteranDesk.
Supports PostgreSQL / Supabase and local SQLite fallback.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:
    from supabase import create_client, Client
except ImportError:  # pragma: no cover
    Client = None  # type: ignore
    create_client = None  # type: ignore

from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.database")


class DatabaseManager:
    """
    Manages database connection and live health ping.
    Connects to Supabase PostgreSQL when SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set,
    with local SQLite fallback for offline development.
    """

    def __init__(self) -> None:
        self.supabase_url: Optional[str] = settings.supabase_url
        self.supabase_key: Optional[str] = (
            settings.supabase_service_role_key or settings.supabase_key or settings.supabase_anon_key
        )
        self.supabase_client: Optional[Any] = None

        if self.supabase_url and self.supabase_key and create_client is not None:
            try:
                self.supabase_client = create_client(self.supabase_url, self.supabase_key)
                logger.info("supabase_client_initialized", url=self.supabase_url)
            except Exception as e:
                logger.error("supabase_client_init_failed", error=str(e))

        db_url = settings.database_url
        if "+aiosqlite" in db_url:
            self.sync_db_url = db_url.replace("+aiosqlite", "")
        elif "+asyncpg" in db_url:
            self.sync_db_url = db_url.replace("+asyncpg", "")
        else:
            self.sync_db_url = db_url

        self._engine: Optional[Engine] = None

    def get_client(self) -> Any:
        """Return initialized Supabase client instance."""
        if self.supabase_client is not None:
            return self.supabase_client
        if self.supabase_url and self.supabase_key and create_client is not None:
            self.supabase_client = create_client(self.supabase_url, self.supabase_key)
            return self.supabase_client
        raise RuntimeError("Supabase credentials not configured in settings.")

    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.sync_db_url, echo=False)
        return self._engine

    def check_connection(self) -> Dict[str, Any]:
        """
        Execute a live health ping against the database.
        Returns detailed status including provider and latency.
        """
        is_supabase = bool(self.supabase_url and self.supabase_key)
        t0 = time.perf_counter()

        if is_supabase and self.supabase_url and self.supabase_key:
            try:
                headers = {
                    "apikey": self.supabase_key,
                    "Authorization": f"Bearer {self.supabase_key}"
                }
                # Real live health ping to Supabase PostgreSQL PostgREST endpoint
                resp = httpx.get(
                    f"{self.supabase_url.rstrip('/')}/rest/v1/",
                    headers=headers,
                    timeout=5.0
                )
                latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                if resp.status_code == 200:
                    domain = self.supabase_url.split("://")[-1]
                    return {
                        "status": "GREEN",
                        "provider": "Supabase (PostgreSQL)",
                        "supabase_configured": True,
                        "latency_ms": latency_ms,
                        "message": f"Connected to live Supabase PostgreSQL ({domain})"
                    }
                else:
                    return {
                        "status": "RED",
                        "provider": "Supabase (PostgreSQL)",
                        "supabase_configured": True,
                        "latency_ms": latency_ms,
                        "message": f"Supabase responded with status {resp.status_code}: {resp.text}"
                    }
            except Exception as e:
                latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                logger.error("supabase_health_check_failed", error=str(e))
                return {
                    "status": "RED",
                    "provider": "Supabase (PostgreSQL)",
                    "supabase_configured": True,
                    "latency_ms": latency_ms,
                    "message": f"Connection failed: {str(e)}"
                }

        # Fallback to local SQLite
        try:
            engine = self.get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            return {
                "status": "GREEN",
                "provider": "Local SQLite Fallback",
                "supabase_configured": False,
                "latency_ms": latency_ms,
                "message": "Connected to local SQLite database (SUPABASE_URL not configured in .env)"
            }
        except Exception as e:
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            logger.error("database_health_check_failed", error=str(e))
            return {
                "status": "RED",
                "provider": "Local SQLite Fallback",
                "supabase_configured": False,
                "latency_ms": latency_ms,
                "message": f"Connection failed: {str(e)}"
            }


db_manager = DatabaseManager()
get_client = db_manager.get_client

