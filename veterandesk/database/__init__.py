"""Database package for VeteranDesk."""

from veterandesk.database.session import DatabaseManager, db_manager

__all__ = ["DatabaseManager", "db_manager"]
