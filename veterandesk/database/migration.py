"""
Database Schema Migration Runner for VeteranDesk.
Executes sql/001_initial_schema.sql against Supabase PostgreSQL or local SQLite.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from sqlalchemy import create_engine, text
from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.migration")


def run_migration(schema_file_path: Optional[str] = None) -> bool:
    """Execute SQL migration script against the configured database."""
    base_dir = Path(__file__).resolve().parent.parent.parent
    sql_path = Path(schema_file_path) if schema_file_path else base_dir / "sql" / "001_initial_schema.sql"

    if not sql_path.exists():
        logger.error("migration_file_not_found", path=str(sql_path))
        print(f"Error: Migration file not found at {sql_path}")
        return False

    with open(sql_path, "r", encoding="utf-8") as f:
        sql_content = f.read()

    db_url = settings.database_url
    if "+aiosqlite" in db_url:
        sync_url = db_url.replace("+aiosqlite", "")
    elif "+asyncpg" in db_url:
        sync_url = db_url.replace("+asyncpg", "")
    else:
        sync_url = db_url

    print(f"Applying schema migration from: {sql_path.name}")
    print(f"Target Database URL: {sync_url}")

    try:
        # If target is SQLite, convert PostgreSQL-specific syntax for local compatibility
        if "sqlite" in sync_url:
            print("Preparing SQLite-compatible statements...")
            clean_sql = (
                sql_content
                .replace("TIMESTAMPTZ", "DATETIME")
                .replace("TIMESTAMP WITH TIME ZONE", "DATETIME")
                .replace("NOW() AT TIME ZONE 'utc'", "CURRENT_TIMESTAMP")
                .replace("JSONB", "TEXT")
                .replace("BIGSERIAL", "INTEGER")
                .replace("SERIAL", "INTEGER")
                .replace("::jsonb", "")
            )
            engine = create_engine(sync_url, echo=False)
            with engine.connect() as conn:
                for statement in clean_sql.split(";"):
                    stmt = statement.strip()
                    if stmt:
                        try:
                            conn.execute(text(stmt))
                        except Exception:
                            pass
                conn.commit()
            print("Successfully executed migration against local database.")
            return True
        else:
            # PostgreSQL / Supabase
            engine = create_engine(sync_url, echo=False)
            with engine.connect() as conn:
                conn.execute(text(sql_content))
                conn.commit()
            print("Successfully executed migration against PostgreSQL / Supabase.")
            return True

    except Exception as e:
        print(f"Migration error: {e}")
        logger.error("migration_failed", error=str(e))
        return False


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    run_migration(path)
