"""
Pytest configuration and fixtures for tests.
"""

import pytest
from unittest.mock import patch, MagicMock

from veterandesk.config import settings
from veterandesk.alerts.telegram import telegram_service
from veterandesk.alerts.discord import discord_service


@pytest.fixture
def deterministic_llm() -> None:
    """
    Force deterministic LLM fallback for post-mortem tests.
    
    This ensures tests are deterministic and don't depend on real LLM API responses,
    which can be non-deterministic and flaky. The deterministic fallback has
    well-defined rules that produce consistent verdicts.
    
    Apply this fixture explicitly to tests that need deterministic behavior.
    """
    with patch.object(settings, 'use_mock_llm_if_no_key', True):
        yield


@pytest.fixture(autouse=True)
def mock_database_persistence() -> None:
    """
    Mock database persistence to avoid foreign key constraint violations in tests.
    
    Tests create journal records without corresponding trade records in the database,
    which causes foreign key violations. This fixture mocks the persistence layer
    to avoid these issues and keep tests isolated from the database.
    """
    with patch('veterandesk.database.session.db_manager') as mock_db_mgr:
        mock_client = MagicMock()
        mock_table = MagicMock()
        mock_client.table.return_value = mock_table
        mock_table.upsert.return_value.execute.return_value = None
        mock_table.insert.return_value.execute.return_value = None
        mock_db_mgr.get_client.return_value = mock_client
        yield


@pytest.fixture(autouse=True)
def disable_real_network_requests() -> None:
    """
    Disable real Telegram, Discord, and Groq API calls in tests.
    
    This prevents tests from making real network requests even if mocks fail to apply,
    ensuring test isolation and preventing side effects.
    
    Patches settings and get_secret to block environment variable reads, AND patches
    the already-instantiated telegram_service/discord_service singletons directly —
    those singletons freeze their .enabled flag at import time (before this fixture
    ever runs), so patching settings alone does not affect them.
    """
    def mock_get_secret(key: str, default: str = None) -> str:
        """Mock get_secret to always return the default, blocking environment reads."""
        return default
    
    with patch.object(settings, 'telegram_enabled', False), \
         patch.object(settings, 'discord_enabled', False), \
         patch.object(settings, 'groq_api_key', None), \
         patch('veterandesk.config.get_secret', side_effect=mock_get_secret), \
         patch.object(telegram_service, 'enabled', False), \
         patch.object(discord_service, 'enabled', False):
        yield
