import io
from datetime import date
import pandas as pd
import openpyxl
import pytest

from veterandesk.dashboard.export import (
    TRADE_EXPORT_COLUMNS,
    build_trade_log_dataframe,
    build_ledger_dataframe,
    build_mistakes_dataframe,
    generate_excel_export,
    generate_csv_export,
)


def test_empty_trade_log_dataframe():
    df = build_trade_log_dataframe([], [], [])
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == TRADE_EXPORT_COLUMNS
    assert len(df) == 0

    excel_bytes = generate_excel_export(df)
    assert len(excel_bytes) > 0
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes))
    ws = wb["Trade_Log"]
    headers = [cell.value for cell in ws[1]]
    assert headers == TRADE_EXPORT_COLUMNS

    csv_bytes = generate_csv_export(df)
    assert len(csv_bytes) > 0
    csv_str = csv_bytes.decode("utf-8")
    assert "Trade ID,Ticker" in csv_str


def test_joined_trade_log_dataframe():
    trades = [
        {
            "trade_id": "TRD_001",
            "ticker": "OGDC",
            "action": "BUY",
            "shares": 100,
            "entry_price": 150.00,
            "exit_price": 154.50,
            "stop_loss": 147.00,
            "target_price": 155.00,
            "net_pnl": 450.00,
            "status": "CLOSED",
            "exit_reason": "TARGET_HIT",
            "opened_at": "2026-09-07T09:35:00+05:00",
            "closed_at": "2026-09-07T11:00:00+05:00",
        },
        {
            "trade_id": "TRD_002",
            "ticker": "HBL",
            "action": "BUY",
            "shares": 200,
            "entry_price": 110.00,
            "exit_price": None,
            "stop_loss": 108.00,
            "target_price": 114.00,
            "net_pnl": None,
            "status": "OPEN",
            "opened_at": "2026-09-07T10:15:00+05:00",
        }
    ]

    journals = [
        {
            "trade_id": "TRD_001",
            "verdict": "Right",
            "post_mortem_analysis": "High volume breakout on opening bar.",
            "transferable_lesson": "Wait for volume confirmation.",
        }
    ]

    mistakes = [
        {
            "trade_id": "TRD_001",
            "rule_violated": "RISK_PER_TRADE_CHECK",
            "severity": "WARNING",
            "details": "Approached 1.00% ceiling",
        }
    ]

    df = build_trade_log_dataframe(trades, journals, mistakes)
    assert len(df) == 2

    row1 = df[df["Trade ID"] == "TRD_001"].iloc[0]
    assert row1["Ticker"] == "OGDC"
    assert row1["Signal Type"] == "BUY"
    assert row1["Position Size (Shares)"] == 100
    assert row1["Entry Price (PKR)"] == 150.00
    assert row1["Exit Price (PKR)"] == 154.50
    assert row1["Net PnL (PKR)"] == 450.00
    # Cost basis = 150 * 100 = 15000. Net PnL % = 450 / 15000 * 100 = 3.0%
    assert row1["Net PnL (%)"] == 3.0
    assert row1["Verdict"] == "Right"
    assert "High volume breakout" in row1["Post-Mortem Analysis"]
    assert "Wait for volume confirmation" in row1["Transferable Lesson"]
    assert "[WARNING] RISK_PER_TRADE_CHECK" in row1["Mistake Flags"]
    assert row1["Status"] == "CLOSED"

    row2 = df[df["Trade ID"] == "TRD_002"].iloc[0]
    assert row2["Ticker"] == "HBL"
    assert pd.isna(row2["Net PnL (PKR)"])
    assert pd.isna(row2["Net PnL (%)"])
    assert row2["Verdict"] == "Open"
    assert row2["Mistake Flags"] == "None"
    assert row2["Status"] == "OPEN"


def test_filters():
    trades = [
        {
            "trade_id": "TRD_A",
            "ticker": "OGDC",
            "action": "BUY",
            "shares": 50,
            "entry_price": 100.0,
            "opened_at": "2026-09-01T10:00:00+05:00",
            "status": "CLOSED",
        },
        {
            "trade_id": "TRD_B",
            "ticker": "HBL",
            "action": "BUY",
            "shares": 50,
            "entry_price": 100.0,
            "opened_at": "2026-09-05T10:00:00+05:00",
            "status": "CLOSED",
        },
        {
            "trade_id": "TRD_C",
            "ticker": "LUCK",
            "action": "BUY",
            "shares": 50,
            "entry_price": 100.0,
            "opened_at": "2026-09-07T10:00:00+05:00",
            "status": "CLOSED",
        }
    ]
    journals = [
        {"trade_id": "TRD_A", "verdict": "Right"},
        {"trade_id": "TRD_B", "verdict": "Wrong"},
        {"trade_id": "TRD_C", "verdict": "Wrong-for-right-reason"},
    ]

    # Date filter
    df_date = build_trade_log_dataframe(trades, journals, [], start_date=date(2026, 9, 2), end_date=date(2026, 9, 6))
    assert len(df_date) == 1
    assert df_date.iloc[0]["Trade ID"] == "TRD_B"

    # Ticker filter
    df_ticker = build_trade_log_dataframe(trades, journals, [], ticker_filter="LUCK")
    assert len(df_ticker) == 1
    assert df_ticker.iloc[0]["Trade ID"] == "TRD_C"

    # Verdict filter
    df_verdict = build_trade_log_dataframe(trades, journals, [], verdict_filter="Wrong-for-right-reason")
    assert len(df_verdict) == 1
    assert df_verdict.iloc[0]["Trade ID"] == "TRD_C"


def test_ledger_and_mistakes_dataframe():
    ledger = [
        {
            "id": 1,
            "transaction_id": "TX_01",
            "trade_id": "TRD_01",
            "account_name": "CASH",
            "debit": 0.0,
            "credit": 15000.0,
            "balance_after": 485000.0,
            "description": "BUY OGDC",
            "created_at": "2026-09-07T10:00:00+05:00",
        }
    ]
    df_ledger = build_ledger_dataframe(ledger)
    assert len(df_ledger) == 1
    assert df_ledger.iloc[0]["Account Name"] == "CASH"
    excel_ledg = generate_excel_export(df_ledger, sheet_name="Ledger")
    assert len(excel_ledg) > 0

    mistakes = [
        {
            "id": 10,
            "trade_id": "TRD_01",
            "rule_violated": "NO_STOP_LOSS",
            "severity": "CRITICAL",
            "details": "Trade entered without SL",
            "detected_at": "2026-09-07T10:00:00+05:00",
            "acknowledged": False,
        }
    ]
    df_mistakes = build_mistakes_dataframe(mistakes)
    assert len(df_mistakes) == 1
    assert df_mistakes.iloc[0]["Rule Violated"] == "NO_STOP_LOSS"
    csv_mistakes = generate_csv_export(df_mistakes)
    assert b"NO_STOP_LOSS" in csv_mistakes
