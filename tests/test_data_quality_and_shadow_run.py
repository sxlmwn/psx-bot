"""
Unit tests for data quality validation, flagging, and official shadow-run checkpoint behavior.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from veterandesk.alerts.scheduler import fetch_session_summary_metrics
from veterandesk.dashboard.export import (
    TRADE_EXPORT_COLUMNS,
    build_trade_log_dataframe,
)
from veterandesk.execution.graduation import compute_performance_metrics
from veterandesk.execution.paper_broker import (
    DemoTrade,
    ExitReason,
    PaperBroker,
    TradeStatus,
)
from veterandesk.strategy.models import SignalAction



def test_graduation_metrics_excludes_invalid_and_pre_shadow_run_trades() -> None:
    """
    Test that compute_performance_metrics filters out:
    1. Trades where is_valid_signal=False or data_quality_flag='INVALID'.
    2. Trades opened before shadow_run_official_start_date (2026-09-15).
    """
    trades = [
        # Pre-shadow run trade (2026-09-14) - Valid signal but before official start
        {
            "trade_id": "TRD_PRE_01",
            "ticker": "MCB",
            "status": "CLOSED",
            "opened_at": "2026-09-14T04:30:00+00:00",
            "closed_at": "2026-09-14T05:00:00+00:00",
            "entry_price": 200.0,
            "exit_price": 204.0,
            "shares": 100,
            "net_pnl": 400.0,
            "is_valid_signal": True,
            "data_quality_flag": "VALID",
        },
        # Flagged invalid trade on 2026-09-14 (OGDC stale candle incident)
        {
            "trade_id": "TRD_OGDC_1789359317_9f61a7",
            "ticker": "OGDC",
            "status": "CLOSED",
            "opened_at": "2026-09-14T04:15:17+00:00",
            "closed_at": "2026-09-14T04:16:27+00:00",
            "entry_price": 315.79,
            "exit_price": 319.0,
            "shares": 1000,
            "net_pnl": 2687.51,
            "is_valid_signal": False,
            "data_quality_flag": "INVALID",
            "invalidation_reason": "stale_candle_data_pre_fix",
        },
        # Official Shadow Run Trade 1 (2026-09-15) - Win
        {
            "trade_id": "TRD_DAY1_01",
            "ticker": "PPL",
            "status": "CLOSED",
            "opened_at": "2026-09-15T04:45:00+00:00",
            "closed_at": "2026-09-15T05:15:00+00:00",
            "entry_price": 120.0,
            "exit_price": 123.0,
            "shares": 500,
            "net_pnl": 1500.0,
            "is_valid_signal": True,
            "data_quality_flag": "VALID",
        },
        # Official Shadow Run Trade 2 (2026-09-15) - Loss
        {
            "trade_id": "TRD_DAY1_02",
            "ticker": "ENGRO",
            "status": "CLOSED",
            "opened_at": "2026-09-15T05:00:00+00:00",
            "closed_at": "2026-09-15T05:30:00+00:00",
            "entry_price": 300.0,
            "exit_price": 298.0,
            "shares": 200,
            "net_pnl": -400.0,
            "is_valid_signal": True,
            "data_quality_flag": "VALID",
        },
    ]

    metrics = compute_performance_metrics(trades, official_start_date=date(2026, 9, 15))

    # Only TRD_DAY1_01 and TRD_DAY1_02 should be included
    assert metrics.total_trades == 2
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate_pct == 50.0
    assert metrics.total_net_pnl == 1100.0


def test_fetch_session_summary_filters_invalid_trades() -> None:
    """Test that fetch_session_summary_metrics ignores trades marked as invalid."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.get_client.return_value = mock_client

    sample_trades = [
        # Valid trade
        {
            "trade_id": "TRD_VALID_01",
            "ticker": "OGDC",
            "opened_at": "2026-09-15T04:15:00.000Z",
            "gross_pnl": 2000.0,
            "fees_paid": 200.0,
            "net_pnl": 1800.0,
            "is_valid_signal": True,
            "data_quality_flag": "VALID",
        },
        # Invalid trade (e.g. stale signal or manual correction)
        {
            "trade_id": "TRD_INVALID_02",
            "ticker": "LUCK",
            "opened_at": "2026-09-15T04:20:00.000Z",
            "gross_pnl": -500.0,
            "fees_paid": 100.0,
            "net_pnl": -600.0,
            "is_valid_signal": False,
            "data_quality_flag": "INVALID",
            "invalidation_reason": "stale_candle_data_pre_fix",
        },
    ]

    mock_client.table.return_value.select.return_value.gte.return_value.lte.return_value.execute.return_value.data = sample_trades
    mock_client.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        {"balance_after": 501800.0}
    ]

    with patch("veterandesk.database.session.db_manager", mock_db):
        metrics = fetch_session_summary_metrics("2026-09-15")

    # Only the valid trade should be counted
    assert metrics["trades_count"] == 1
    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 0
    assert metrics["gross_pnl"] == 2000.0
    assert metrics["total_fees"] == 200.0
    assert metrics["net_pnl"] == 1800.0


def test_build_trade_log_dataframe_data_quality_filter() -> None:
    """Test trade log export filtering on data quality."""
    trades = [
        {
            "trade_id": "T_VAL",
            "ticker": "OGDC",
            "action": "BUY",
            "shares": 100,
            "entry_price": 315.0,
            "exit_price": 320.0,
            "net_pnl": 500.0,
            "status": "CLOSED",
            "opened_at": "2026-09-15T04:15:00+00:00",
            "is_valid_signal": True,
            "data_quality_flag": "VALID",
            "invalidation_reason": None,
        },
        {
            "trade_id": "T_INVAL",
            "ticker": "LUCK",
            "action": "BUY",
            "shares": 100,
            "entry_price": 408.0,
            "exit_price": 408.0,
            "net_pnl": -306.64,
            "status": "CLOSED",
            "opened_at": "2026-09-14T04:15:00+00:00",
            "is_valid_signal": False,
            "data_quality_flag": "INVALID",
            "invalidation_reason": "stale_candle_data_pre_fix",
        },
    ]

    # Filter: Valid Only
    df_valid = build_trade_log_dataframe(trades, [], [], data_quality_filter="Valid Only")
    assert len(df_valid) == 1
    assert df_valid.iloc[0]["Trade ID"] == "T_VAL"
    assert df_valid.iloc[0]["Data Quality"] == "VALID"
    assert df_valid.iloc[0]["Invalidation Reason"] in ("None", None, "") or pd.isna(df_valid.iloc[0]["Invalidation Reason"])

    # Filter: Invalid Only
    df_invalid = build_trade_log_dataframe(trades, [], [], data_quality_filter="Invalid Only")
    assert len(df_invalid) == 1
    assert df_invalid.iloc[0]["Trade ID"] == "T_INVAL"
    assert df_invalid.iloc[0]["Data Quality"] == "INVALID"
    assert df_invalid.iloc[0]["Invalidation Reason"] == "stale_candle_data_pre_fix"

    # Filter: All (Audit Trail preserves everything)
    df_all = build_trade_log_dataframe(trades, [], [], data_quality_filter="All")
    assert len(df_all) == 2
    assert "Data Quality" in df_all.columns
    assert "Invalidation Reason" in df_all.columns


def test_demo_trade_model_invalidation_fields() -> None:
    """Test DemoTrade defaults and to_db_dict serialization of data quality fields."""
    now = datetime.now(timezone.utc)
    trade = DemoTrade(
        trade_id="TRD_TEST_01",
        signal_id="SIG_TEST_01",
        ticker="HUBC",
        action=SignalAction.BUY,
        shares=100,
        entry_price=200.0,
        stop_loss=195.0,
        target_price=210.0,
        slippage_pct=0.0005,
        filled_entry_price=200.1,
        opened_at=now,
    )
    assert trade.is_valid_signal is True
    assert trade.data_quality_flag == "VALID"
    assert trade.invalidation_reason is None

    db_dict = trade.to_db_dict()
    assert db_dict["is_valid_signal"] is True
    assert db_dict["data_quality_flag"] == "VALID"
    assert db_dict["invalidation_reason"] is None

    # Mark as invalid
    trade.is_valid_signal = False
    trade.data_quality_flag = "INVALID"
    trade.invalidation_reason = "stale_candle_data_pre_fix"

    db_dict_inval = trade.to_db_dict()
    assert db_dict_inval["is_valid_signal"] is False
    assert db_dict_inval["data_quality_flag"] == "INVALID"
    assert db_dict_inval["invalidation_reason"] == "stale_candle_data_pre_fix"
