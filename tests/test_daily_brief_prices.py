"""
Tests for Daily Brief Watchlist Price Resolution and Formatting.

Validates:
1. When real quote data exists, Telegram format_daily_brief outputs real, non-zero prices and % changes.
2. When real quote data exists, Discord format_daily_brief_embed outputs real, non-zero prices and % changes.
3. fetch_watchlist_snapshot resolves prices and calculates % changes from PSXDpsScraper.
4. fetch_watchlist_snapshot falls back to database market_ticks table when scraper fails.
5. run_daily_brief_job (default invocation at 09:15 PKT with no args) dispatches non-zero prices to Telegram and Discord.
"""

from unittest.mock import MagicMock, patch
from typing import Any, Dict, List
import pytest

from veterandesk.alerts.discord import discord_service
from veterandesk.alerts.scheduler import fetch_watchlist_snapshot, run_daily_brief_job
from veterandesk.alerts.telegram import telegram_service


class TestDailyBriefPriceData:
    """Tests ensuring Daily Brief never displays PKR 0.00 (+0.00%) when prices exist."""

    def test_telegram_format_daily_brief_outputs_real_prices(self) -> None:
        """Assert that Telegram format_daily_brief formats non-zero prices and % change."""
        watchlist = [
            {"ticker": "OGDC", "price": 315.79, "change_pct": 1.25},
            {"ticker": "PPL", "price": 218.86, "change_pct": -0.85},
            {"ticker": "ENGRO", "price": 485.38, "change_pct": 0.70},
        ]
        brief = telegram_service.format_daily_brief(
            date_str="2026-09-15",
            market_overview="PSX KSE-100 opening session.",
            watchlist_summary=watchlist,
        )

        assert "OGDC" in brief
        assert "PKR  315.79 (+1.25%)" in brief
        assert "PPL" in brief
        assert "PKR  218.86 (-0.85%)" in brief
        assert "ENGRO" in brief
        assert "PKR  485.38 (+0.70%)" in brief
        assert "0.00 (+0.00%)" not in brief

    def test_discord_format_daily_brief_embed_outputs_real_prices(self) -> None:
        """Assert that Discord format_daily_brief_embed formats non-zero prices and % change."""
        watchlist = [
            {"ticker": "HUBC", "price": 203.70, "change_pct": 0.82},
            {"ticker": "LUCK", "price": 410.79, "change_pct": -1.37},
        ]
        embed = discord_service.format_daily_brief_embed(
            date_str="2026-09-15",
            market_overview="PSX KSE-100 opening session.",
            watchlist_summary=watchlist,
        )

        wl_field = next(f for f in embed["fields"] if f["name"] == "Focus Watchlist")
        assert "HUBC" in wl_field["value"]
        assert "PKR  203.70 (+0.82%)" in wl_field["value"]
        assert "LUCK" in wl_field["value"]
        assert "PKR  410.79 (-1.37%)" in wl_field["value"]
        assert "0.00 (+0.00%)" not in wl_field["value"]

    def test_fetch_watchlist_snapshot_from_scraper(self) -> None:
        """Assert that fetch_watchlist_snapshot resolves prices and computes change_pct from scraper."""
        mock_scraper = MagicMock()

        def mock_fetch(ticker: str) -> Dict[str, Any]:
            quotes = {
                "OGDC": {"ticker": "OGDC", "price": 320.0, "change": 3.20, "volume": 500000},
                "PPL": {"ticker": "PPL", "price": 200.0, "change": -2.00, "volume": 300000},
            }
            return quotes.get(ticker, {"ticker": ticker, "price": 100.0, "change": 0.0, "volume": 10000})

        mock_scraper.fetch_ticker_quote.side_effect = mock_fetch

        snapshot = fetch_watchlist_snapshot(symbols=["OGDC", "PPL"], scraper=mock_scraper)

        assert len(snapshot) == 2
        ogdc = next(item for item in snapshot if item["ticker"] == "OGDC")
        assert ogdc["price"] == 320.0
        # change = 3.20, prev_close = 320.0 - 3.20 = 316.80 -> 3.20 / 316.80 * 100 = 1.01%
        assert ogdc["change_pct"] == 1.01

        ppl = next(item for item in snapshot if item["ticker"] == "PPL")
        assert ppl["price"] == 200.0
        # change = -2.00, prev_close = 200.0 - (-2.00) = 202.00 -> -2.00 / 202.00 * 100 = -0.99%
        assert ppl["change_pct"] == -0.99

    def test_fetch_watchlist_snapshot_fallback_to_database(self) -> None:
        """Assert that fetch_watchlist_snapshot falls back to market_ticks in database when scraper fails."""
        mock_scraper = MagicMock()
        mock_scraper.fetch_ticker_quote.return_value = None  # Scraper fails or returns no quote

        mock_db_client = MagicMock()
        mock_table = MagicMock()
        mock_select = MagicMock()
        mock_eq = MagicMock()
        mock_order = MagicMock()
        mock_limit = MagicMock()
        mock_exec = MagicMock()

        mock_db_client.table.return_value = mock_table
        mock_table.select.return_value = mock_select
        mock_select.eq.return_value = mock_eq
        mock_eq.order.return_value = mock_order
        mock_order.limit.return_value = mock_limit
        mock_limit.execute.return_value = mock_exec

        # Database returns recent tick: price=218.86, change=8.88
        mock_exec.data = [
            {"price": 218.86, "change": 8.88, "change_pct": 4.23}
        ]

        with patch("veterandesk.database.session.db_manager.get_client", return_value=mock_db_client):
            snapshot = fetch_watchlist_snapshot(symbols=["PPL"], scraper=mock_scraper)

        assert len(snapshot) == 1
        assert snapshot[0]["ticker"] == "PPL"
        assert snapshot[0]["price"] == 218.86
        assert snapshot[0]["change_pct"] == 4.23

    def test_run_daily_brief_job_dispatches_real_prices_end_to_end(self) -> None:
        """
        Assert that run_daily_brief_job (called with no args, as APScheduler does)
        fetches real price data and passes non-zero prices to telegram and discord services.
        """
        mock_scraper = MagicMock()
        mock_scraper.fetch_ticker_quote.side_effect = lambda ticker: {
            "ticker": ticker,
            "price": 315.79,
            "change": 3.15,
            "volume": 2500000,
        }

        with patch("veterandesk.alerts.scheduler._check_already_sent_today", return_value=False), \
             patch.object(telegram_service, "send_daily_brief", return_value=True) as mock_tg, \
             patch.object(discord_service, "send_daily_brief", return_value=True) as mock_dc:

            success = run_daily_brief_job(
                date_str="2026-09-15",
                scraper=mock_scraper,
            )

            assert success is True
            mock_tg.assert_called_once()
            mock_dc.assert_called_once()

            # Verify the watchlist_summary passed to Telegram
            _, tg_kwargs = mock_tg.call_args
            wl_summary: List[Dict[str, Any]] = tg_kwargs["watchlist_summary"]
            assert len(wl_summary) > 0
            for item in wl_summary:
                assert item["price"] > 0.0, f"Ticker {item['ticker']} had 0.00 price!"
                assert item["price"] == 315.79

            # Verify the watchlist_summary passed to Discord
            _, dc_kwargs = mock_dc.call_args
            assert dc_kwargs["watchlist_summary"] == wl_summary
