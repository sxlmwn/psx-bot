"""
Tests for FastAPI application endpoints.
"""

import pytest
from fastapi.testclient import TestClient
from veterandesk.api.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_api_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("GREEN", "RED")
    assert "components" in data
    assert "ledger" in data["components"]


def test_api_metrics_endpoint(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "cash_balance" in data
    assert "metrics" in data
    assert "is_graduated" in data["metrics"]


def test_api_trades_endpoints(client):
    resp_open = client.get("/trades/open")
    assert resp_open.status_code == 200
    assert isinstance(resp_open.json(), list)

    resp_closed = client.get("/trades/closed")
    assert resp_closed.status_code == 200
    assert isinstance(resp_closed.json(), list)


def test_api_journal_and_lessons_endpoints(client):
    resp_journal = client.get("/journal")
    assert resp_journal.status_code == 200
    assert isinstance(resp_journal.json(), list)

    resp_lessons = client.get("/lessons")
    assert resp_lessons.status_code == 200
    assert isinstance(resp_lessons.json(), list)
