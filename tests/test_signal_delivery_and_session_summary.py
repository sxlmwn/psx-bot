"""
Unit tests for:
1. Intraday candle session filtering (preventing Friday candles from firing Monday breakouts).
2. Defense-in-depth rejection of stale signals.
3. 15:20 PKT mandatory exit execution even when quote scraper fails.
4. Overnight stale position detection and force-close.
5. TickValidator reset on new trading session.
6. Dynamic session summary metrics retrieval and job execution.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from veterandesk.alerts.scheduler import (
    fetch_session_summary_metrics,
    run_session_summary_job,
)
from veterandesk.config import PKT_TZ
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker, TradeStatus
from veterandesk.market_data.validator import TickValidator
from veterandesk.risk.engine import RiskAssessment, RiskEngine
from veterandesk.strategy.models import SignalAction, SignalStatus, TradeSignal
from veterandesk.trading_engine import TradingEngine


def test_tick_validator_reset_on_new_day() -> None:
    """Test that resetting the tick validator allows low opening volume after high previous day volume."""
    validator = TickValidator()
    # Friday end of day: high cumulative volume
    res1 = validator.validate_tick("LUCK", 300.0, 1_500_000)
    assert res1.is_valid is True

    # Monday morning without reset: volume restarts from low number -> rejected
    res2 = validator.validate_tick("LUCK", 301.0, 5_000)
    assert res2.is_valid is False
    assert res2.status == "rejected"

    # Reset intraday state on new session
    validator.reset()

    # Monday morning with reset: volume restarts from low number -> accepted
    res3 = validator.validate_tick("LUCK", 301.0, 5_000)
    assert res3.is_valid is True
    assert res3.status == "ok"


def test_trading_engine_resets_validator_on_new_day() -> None:
    """Test that TradingEngine._reset_session_if_new_day invokes validator.reset()."""
    engine = TradingEngine()
    engine.scraper.validator._last_ticks["OGDC"] = (300.0, 1_000_000)
    assert "OGDC" in engine.scraper.validator._last_ticks

    # Trigger session reset for new day
    engine._reset_session_if_new_day(date(2026, 9, 14))
    assert len(engine.scraper.validator._last_ticks) == 0


def test_run_trading_cycle_ignores_prior_day_candles() -> None:
    """
    Test that when DPS returns Friday candles on Monday morning at 09:15 PKT,
    the engine filters out Friday candles and does NOT execute any trades.
    """
    ledger = DoubleEntryLedger(starting_balance_pkr=500_000.0)
    broker = PaperBroker(ledger=ledger, persist_to_db=False)
    risk_engine = RiskEngine()
    scraper = MagicMock()

    engine = TradingEngine(ledger=ledger, broker=broker, risk_engine=risk_engine, scraper=scraper)

    # Monday morning 09:15 PKT
    monday_0915_pkt = datetime(2026, 9, 14, 9, 15, 0, tzinfo=PKT_TZ)

    # Scraper returns Friday candles (2026-09-11)
    friday_dt = datetime(2026, 9, 11, 4, 15, 0, tzinfo=timezone.utc)
    friday_candles = []
    for i in range(20):
        friday_candles.append({
            "ticker": "OGDC",
            "timeframe": "1m",
            "open": 315.0,
            "high": 316.0,
            "low": 314.0,
            "close": 315.5,
            "volume": 10000,
            "timestamp": friday_dt + timedelta(minutes=i),
            "data_status": "ok",
        })

    scraper.fetch_intraday_data.return_value = (
        {"ticker": "OGDC", "price": 315.5, "volume": 200000, "data_status": "ok"},
        friday_candles,
    )
    scraper.fetch_ticker_quote.return_value = None

    with patch.object(engine, "_is_already_halted_today", return_value=False), \
         patch("veterandesk.trading_engine.settings.watchlist", ["OGDC"]):
        result = engine.run_trading_cycle(force_scan=True, now_pkt=monday_0915_pkt)

    # No trades executed because today has 0 candles (< 16 needed)
    assert result["trades_executed"] == 0
    assert result["signals_fired"] == 0
    assert len(broker.open_trades) == 0


def test_run_trading_cycle_accepts_today_candles_and_executes() -> None:
    """
    Test that when today's candles are available, the engine detects breakout
    and executes trade normally.
    """
    ledger = DoubleEntryLedger(starting_balance_pkr=500_000.0)
    broker = PaperBroker(ledger=ledger, persist_to_db=False)
    risk_engine = RiskEngine()
    scraper = MagicMock()

    engine = TradingEngine(ledger=ledger, broker=broker, risk_engine=risk_engine, scraper=scraper)

    # Monday 09:35 PKT
    monday_now = datetime(2026, 9, 14, 9, 35, 0, tzinfo=PKT_TZ)
    monday_start_utc = datetime(2026, 9, 14, 4, 15, 0, tzinfo=timezone.utc)

    today_candles = []
    for i in range(15):
        today_candles.append({
            "ticker": "OGDC",
            "timeframe": "1m",
            "open": 315.0,
            "high": 316.0,
            "low": 314.0,
            "close": 315.0,
            "volume": 10000,
            "timestamp": monday_start_utc + timedelta(minutes=i),
            "data_status": "ok",
        })
    # 16th candle: Breakout above 316.0 with 30,000 volume (>1.5x)
    today_candles.append({
        "ticker": "OGDC",
        "timeframe": "1m",
        "open": 315.5,
        "high": 317.5,
        "low": 315.0,
        "close": 317.0,
        "volume": 30000,
        "timestamp": monday_start_utc + timedelta(minutes=15),
        "data_status": "ok",
    })

    scraper.fetch_intraday_data.return_value = (
        {"ticker": "OGDC", "price": 317.0, "volume": 180000, "data_status": "ok"},
        today_candles,
    )
    scraper.fetch_ticker_quote.return_value = None

    with patch.object(engine, "_is_already_halted_today", return_value=False), \
         patch("veterandesk.trading_engine.settings.watchlist", ["OGDC"]), \
         patch("veterandesk.alerts.telegram.telegram_service.send_signal_alert"), \
         patch("veterandesk.alerts.discord.discord_service.send_signal_alert"):

        result = engine.run_trading_cycle(force_scan=True, now_pkt=monday_now)

    assert result["trades_executed"] == 1
    assert "OGDC" in engine.tickers_traded_today


def test_stale_signal_timestamp_rejected_by_engine() -> None:
    """Test defense-in-depth: if compute_orb_signal returns a signal with stale date, reject it."""
    ledger = DoubleEntryLedger(starting_balance_pkr=500_000.0)
    broker = PaperBroker(ledger=ledger, persist_to_db=False)
    risk_engine = RiskEngine()
    scraper = MagicMock()

    engine = TradingEngine(ledger=ledger, broker=broker, risk_engine=risk_engine, scraper=scraper)

    monday_now = datetime(2026, 9, 14, 9, 35, 0, tzinfo=PKT_TZ)

    # Candles that pass today's date filter
    today_candles = []
    for i in range(16):
        today_candles.append({
            "ticker": "OGDC",
            "timeframe": "1m",
            "open": 315.0,
            "high": 316.0,
            "low": 314.0,
            "close": 315.0,
            "volume": 10000,
            "timestamp": datetime(2026, 9, 14, 4, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=i),
            "data_status": "ok",
        })

    scraper.fetch_intraday_data.return_value = (
        {"ticker": "OGDC", "price": 317.0, "volume": 180000, "data_status": "ok"},
        today_candles,
    )

    # Stale signal from Friday 2026-09-11
    stale_sig = TradeSignal(
        signal_id="SIG_OGDC_STALE",
        ticker="OGDC",
        strategy="ORB_v1.0",
        strategy_version="1.0.0",
        action=SignalAction.BUY,
        entry_price=317.0,
        stop_loss=314.0,
        target_price=321.0,
        reward_risk_ratio=1.33,
        position_size=100,
        confidence_pct=70,
        invalidation_reason="Test stale",
        data_status="ok",
        status=SignalStatus.GENERATED,
        created_at=datetime(2026, 9, 11, 5, 58, 0, tzinfo=timezone.utc),
        session_id="default",
    )

    with patch("veterandesk.trading_engine.compute_orb_signal", return_value=stale_sig), \
         patch.object(engine, "_is_already_halted_today", return_value=False), \
         patch("veterandesk.trading_engine.settings.watchlist", ["OGDC"]):

        result = engine.run_trading_cycle(force_scan=True, now_pkt=monday_now)

    # Stale signal was rejected by date check, no trades executed
    assert result["trades_executed"] == 0


def test_force_close_at_1520_when_scraper_fails() -> None:
    """Test that at 15:20 PKT, positions are force closed even if scraper quote fetch returns None."""
    ledger = DoubleEntryLedger(starting_balance_pkr=500_000.0)
    broker = PaperBroker(ledger=ledger, persist_to_db=False)
    scraper = MagicMock()
    engine = TradingEngine(ledger=ledger, broker=broker, scraper=scraper)

    # Open a trade at 10:00 PKT today
    trade = DemoTrade(
        trade_id="TRD_LUCK_TEST",
        signal_id="SIG_LUCK_TEST",
        ticker="LUCK",
        action=SignalAction.BUY,
        shares=100,
        entry_price=300.0,
        stop_loss=290.0,
        target_price=320.0,
        slippage_pct=0.002,
        filled_entry_price=300.6,
        status=TradeStatus.OPEN,
        opened_at=datetime(2026, 9, 14, 5, 0, 0, tzinfo=timezone.utc),
    )
    broker.open_trades[trade.trade_id] = trade

    # Scraper fails and returns None
    scraper.fetch_ticker_quote.return_value = None

    # Time is 15:21 PKT (past 15:20 cutoff)
    time_1521_pkt = datetime(2026, 9, 14, 15, 21, 0, tzinfo=PKT_TZ)

    closed = engine.check_open_positions_for_exits(time_1521_pkt)

    assert len(closed) == 1
    assert closed[0].trade_id == "TRD_LUCK_TEST"
    assert closed[0].exit_reason == ExitReason.TIME_STOP_1520
    assert closed[0].status == TradeStatus.CLOSED
    assert len(broker.open_trades) == 0


def test_stale_overnight_position_force_closed() -> None:
    """Test that an open position held overnight from a previous session is immediately force-closed."""
    ledger = DoubleEntryLedger(starting_balance_pkr=500_000.0)
    broker = PaperBroker(ledger=ledger, persist_to_db=False)
    scraper = MagicMock()
    engine = TradingEngine(ledger=ledger, broker=broker, scraper=scraper)

    # Open trade from yesterday (2026-09-11 Friday)
    trade = DemoTrade(
        trade_id="TRD_OVERNIGHT",
        signal_id="SIG_OVERNIGHT",
        ticker="HUBC",
        action=SignalAction.BUY,
        shares=200,
        entry_price=120.0,
        stop_loss=115.0,
        target_price=130.0,
        slippage_pct=0.002,
        filled_entry_price=120.24,
        status=TradeStatus.OPEN,
        opened_at=datetime(2026, 9, 11, 4, 30, 0, tzinfo=timezone.utc),
    )
    broker.open_trades[trade.trade_id] = trade

    # Current time is Monday 09:15 PKT (market open)
    time_monday_open = datetime(2026, 9, 14, 9, 15, 0, tzinfo=PKT_TZ)

    scraper.fetch_ticker_quote.return_value = {"ticker": "HUBC", "price": 121.0, "volume": 1000}

    closed = engine.check_open_positions_for_exits(time_monday_open)

    assert len(closed) == 1
    assert closed[0].trade_id == "TRD_OVERNIGHT"
    assert closed[0].exit_reason == ExitReason.TIME_STOP_1520
    assert len(broker.open_trades) == 0


def test_fetch_session_summary_metrics_aggregates_correctly() -> None:
    """Test fetch_session_summary_metrics aggregates trades from database."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.get_client.return_value = mock_client

    sample_trades = [
        {
            "trade_id": "T1",
            "ticker": "OGDC",
            "opened_at": "2026-09-14T04:15:17.920Z",
            "gross_pnl": 3000.0,
            "fees_paid": 500.0,
            "net_pnl": 2500.0,
        },
        {
            "trade_id": "T2",
            "ticker": "LUCK",
            "opened_at": "2026-09-14T04:30:00.000Z",
            "gross_pnl": -1000.0,
            "fees_paid": 300.0,
            "net_pnl": -1300.0,
        },
    ]

    mock_client.table.return_value.select.return_value.gte.return_value.lte.return_value.execute.return_value.data = sample_trades
    mock_client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        {"balance_after": 501200.0}
    ]

    with patch("veterandesk.database.session.db_manager", mock_db):
        metrics = fetch_session_summary_metrics("2026-09-14")

    assert metrics["trades_count"] == 2
    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["gross_pnl"] == 2000.0
    assert metrics["total_fees"] == 800.0
    assert metrics["net_pnl"] == 1200.0
    assert metrics["ending_cash"] == 501200.0


def test_run_session_summary_job_fetches_when_none() -> None:
    """Test that run_session_summary_job fetches from DB when invoked with trades_count=None."""
    fake_metrics = {
        "session_date": "2026-09-14",
        "trades_count": 3,
        "winning_trades": 2,
        "losing_trades": 1,
        "gross_pnl": 4000.0,
        "total_fees": 1000.0,
        "net_pnl": 3000.0,
        "discipline_violations": 0,
        "ending_cash": 503000.0,
    }

    with patch("veterandesk.alerts.scheduler._check_already_sent_today", return_value=False), \
         patch("veterandesk.alerts.scheduler.fetch_session_summary_metrics", return_value=fake_metrics) as mock_fetch, \
         patch("veterandesk.alerts.telegram.telegram_service.send_session_summary", return_value=True) as mock_tg, \
         patch("veterandesk.alerts.discord.discord_service.send_session_summary", return_value=True) as mock_dc:

        # Invocation without trade arguments (as APScheduler does)
        ok = run_session_summary_job(session_date="2026-09-14")

        assert ok is True
        mock_fetch.assert_called_once_with(session_date="2026-09-14")
        mock_tg.assert_called_once_with(
            session_date="2026-09-14",
            trades_count=3,
            winning_trades=2,
            losing_trades=1,
            gross_pnl=4000.0,
            total_fees=1000.0,
            net_pnl=3000.0,
            discipline_violations=0,
            ending_cash=503000.0,
        )
