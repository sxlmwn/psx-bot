"""
Pytest configuration and fixtures for tests.
"""

import pytest
from unittest.mock import patch, MagicMock

from veterandesk.config import settings


@pytest.fixture(autouse=True)
def force_deterministic_llm_for_tests():
    """
    Force deterministic LLM fallback for all post-mortem tests.
    
    This ensures tests are deterministic and don't depend on real LLM API responses,
    which can be non-deterministic and flaky. The deterministic fallback has
    well-defined rules that produce consistent verdicts.
    """
    with patch.object(settings, 'use_mock_llm_if_no_key', True):
        yield


@pytest.fixture(autouse=True)
def mock_database_persistence():
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
