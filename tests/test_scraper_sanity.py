"""
Tests for Market Data Module:
- Latency monitoring (>90s)
- Tick sanity validation (price > 0, monotonic volume, >10% jumps)
- Gap and outage detection (2 warnings, 5 halts)
- Candle builder and tick replay
- EOD session integrity check
"""

from datetime import datetime, timedelta, timezone
import pytest

from veterandesk.market_data.validator import TickValidator
from veterandesk.market_data.latency import evaluate_latency
from veterandesk.market_data.gap_detector import GapDetector
from veterandesk.market_data.candle_builder import build_candles_from_ticks
from veterandesk.market_data.integrity import check_session_integrity


class TestMarketDataSanityAndLatency:
    def test_tick_validator_positive_price(self):
        v = TickValidator()
        # Price <= 0 -> Rejected
        assert v.validate_tick("OGDC", 0.0, 1000).is_valid is False
        assert v.validate_tick("OGDC", -50.0, 1000).is_valid is False
        # Negative volume -> Rejected
        assert v.validate_tick("OGDC", 100.0, -10).is_valid is False
        # Valid tick -> OK
        res = v.validate_tick("OGDC", 100.0, 5000)
        assert res.is_valid is True
        assert res.status == "ok"

    def test_tick_validator_monotonic_volume(self):
        v = TickValidator()
        v.validate_tick("PPL", 100.0, 50000)
        # Volume drops from 50,000 to 49,000 -> Non-monotonic -> Rejected
        res = v.validate_tick("PPL", 100.5, 49000)
        assert res.is_valid is False
        assert "Non-monotonic volume" in res.reason

        # Volume increases -> Valid
        res_ok = v.validate_tick("PPL", 100.5, 52000)
        assert res_ok.is_valid is True

    def test_tick_validator_unconfirmed_price_jump(self):
        v = TickValidator()
        v.validate_tick("LUCK", 500.0, 10000)
        # 12% jump with only +10 shares -> Degraded/Suspicious
        res = v.validate_tick("LUCK", 560.0, 10010, volume_delta_min_confirm=1000)
        assert res.is_valid is False
        assert res.status == "degraded"
        assert "Suspicious" in res.reason

    def test_latency_monitor(self):
        now = datetime.now(timezone.utc)
        # Normal latency: 30s -> Acceptable
        res_ok = evaluate_latency(psx_timestamp=now - timedelta(seconds=30), scraped_at=now, max_latency_seconds=90.0)
        assert res_ok.is_acceptable is True
        assert res_ok.status == "ok"

        # High latency: 95s -> Degraded + Alert
        res_high = evaluate_latency(psx_timestamp=now - timedelta(seconds=95), scraped_at=now, max_latency_seconds=90.0)
        assert res_high.is_acceptable is False
        assert res_high.status == "degraded"
        assert res_high.alert_required is True

    def test_gap_detector_alert_and_halt_triggers(self):
        detector = GapDetector(alert_threshold=2, halt_threshold=5)

        # 1st failure -> no alert
        s1 = detector.record_failure("HTTP 500")
        assert s1.consecutive_failures == 1
        assert s1.is_halt_triggered is False
        assert s1.alert_message is None

        # 2nd failure -> Warning alert
        s2 = detector.record_failure("HTTP 502")
        assert s2.consecutive_failures == 2
        assert s2.is_halt_triggered is False
        assert s2.alert_message is not None
        assert "WARNING" in s2.alert_message

        # 3rd & 4th
        detector.record_failure("HTTP 504")
        detector.record_failure("Timeout")

        # 5th failure -> CRITICAL HALT
        s5 = detector.record_failure("Connection refused")
        assert s5.consecutive_failures == 5
        assert s5.is_halt_triggered is True
        assert s5.alert_message is not None
        assert "CRITICAL" in s5.alert_message
        assert "Data unreliable" in s5.alert_message

        # Recovery clears streak
        rec = detector.record_success()
        assert rec.consecutive_failures == 0
        assert rec.is_halt_triggered is False
        assert "restored" in rec.alert_message

    def test_candle_builder_and_replay_capability(self):
        """Replay stored ticks to rebuild 1-minute and 5-minute candles."""
        base_time = datetime(2026, 8, 1, 9, 15, tzinfo=timezone.utc)
        ticks = [
            {"ticker": "OGDC", "price": 100.0, "volume": 1000, "psx_timestamp": base_time, "data_status": "ok"},
            {"ticker": "OGDC", "price": 101.0, "volume": 1500, "psx_timestamp": base_time + timedelta(seconds=20), "data_status": "ok"},
            {"ticker": "OGDC", "price": 99.5, "volume": 2000, "psx_timestamp": base_time + timedelta(seconds=40), "data_status": "ok"},
            {"ticker": "OGDC", "price": 100.5, "volume": 2500, "psx_timestamp": base_time + timedelta(seconds=55), "data_status": "ok"},
        ]

        candles = build_candles_from_ticks(ticks, timeframe_minutes=1, timeframe_label="1m")
        assert len(candles) == 1
        c = candles[0]
        assert c.open == 100.0
        assert c.high == 101.0
        assert c.low == 99.5
        assert c.close == 100.5
        assert c.volume == 1500  # 2500 - 1000
        assert c.data_status == "ok"

    def test_eod_candle_integrity_check(self):
        start = datetime(2026, 8, 1, 9, 15, tzinfo=timezone.utc)
        end = start + timedelta(minutes=10)

        # 10 candles covering all minutes
        candles = []
        for i in range(10):
            from veterandesk.market_data.candle_builder import Candle
            candles.append(Candle(
                ticker="OGDC",
                timeframe="1m",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
                timestamp=start + timedelta(minutes=i),
                data_status="ok"
            ))

        report = check_session_integrity(candles, session_start=start, session_end=end)
        assert report.is_healthy is True
        assert report.actual_candles == 10
        assert len(report.missing_intervals) == 0

    def test_psx_dps_scraper_fetch_and_parse(self):
        from unittest.mock import MagicMock, patch
        from veterandesk.market_data.scraper import PSXDpsScraper

        scraper = PSXDpsScraper(max_retries=1)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {
            "current": 142.50,
            "volume": 2500000,
            "high": 144.0,
            "low": 141.0,
            "change": 2.50,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        with patch.object(scraper.session, "get", return_value=mock_resp):
            tick = scraper.fetch_ticker_quote("OGDC")
            assert tick is not None
            assert tick["ticker"] == "OGDC"
            assert tick["price"] == 142.50
            assert tick["volume"] == 2500000
            assert tick["data_status"] == "ok"

    def test_psx_dps_scraper_retry_on_failure(self):
        from unittest.mock import patch
        from veterandesk.market_data.scraper import PSXDpsScraper

        scraper = PSXDpsScraper(max_retries=2, timeout=0.1)

        with patch.object(scraper.session, "get", side_effect=Exception("Connection timed out")):
            tick = scraper.fetch_ticker_quote("PPL")
            assert tick is None
            assert scraper.gap_detector.consecutive_failures == 1
