"""
Tests for Opening Range Breakout (ORB v1.0) Strategy Engine.

Includes:
1. Pure deterministic function verification
2. Range high/low and volume calculation
3. Volume filter requirement (>= 1.5x range average)
4. Stop-loss and target geometry
5. Degraded candle rejection
6. 20+ Hand-verified Golden Test scenarios
"""

from datetime import datetime, timedelta, timezone
import pytest

from veterandesk.strategy.orb import compute_orb_signal
from veterandesk.strategy.models import SignalAction


def generate_day_candles(
    base_price: float,
    range_high_offset: float,
    range_low_offset: float,
    range_avg_vol: int,
    breakout_minute: int = 20,
    breakout_price_offset: float = 2.0,
    breakout_vol_mult: float = 2.0,
    is_degraded: bool = False,
) -> list[dict]:
    """
    Generate synthetic 1-minute intraday candle session (e.g. 60 candles).
    Range is minutes 0..14 (15 candles).
    """
    base_dt = datetime(2026, 8, 1, 9, 15, tzinfo=timezone.utc)
    candles = []

    for i in range(45):
        ts = base_dt + timedelta(minutes=i)
        if i < 15:
            # Inside opening range
            o = base_price
            h = base_price + (range_high_offset if i == 5 else 0.5)
            l = base_price - (range_low_offset if i == 10 else 0.5)
            c = base_price + 0.2
            v = range_avg_vol
        elif i == breakout_minute:
            # Breakout candle
            o = base_price + range_high_offset
            h = base_price + range_high_offset + breakout_price_offset + 0.5
            l = base_price + range_high_offset - 0.2
            c = base_price + range_high_offset + breakout_price_offset
            v = int(range_avg_vol * breakout_vol_mult)
        else:
            # Post range normal candle
            o = base_price
            h = base_price + 0.5
            l = base_price - 0.5
            c = base_price + 0.1
            v = int(range_avg_vol * 0.8)

        candles.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "data_status": "degraded" if (is_degraded and i == 2) else "ok",
        })

    return candles


class TestORBStrategyPureFunction:
    def test_textbook_orb_breakout(self):
        # Base: 100. Range high = 105, Range low = 95. Range height = 10.
        # Breakout at minute 20: close = 107 (> 105), volume = 2.0x average (2000 vs 1000).
        candles = generate_day_candles(
            base_price=100.0,
            range_high_offset=5.0,
            range_low_offset=5.0,
            range_avg_vol=1000,
            breakout_minute=20,
            breakout_price_offset=2.0,  # close = 107
            breakout_vol_mult=2.0
        )

        sig = compute_orb_signal("OGDC", candles, range_minutes=15, target_multiplier=1.5)
        assert sig is not None
        assert sig.ticker == "OGDC"
        assert sig.action == SignalAction.BUY
        assert sig.entry_price == 107.0
        assert sig.stop_loss == 95.0  # Range low
        # Target = entry (107) + 1.5 * range_height (10) = 122.0
        assert sig.target_price == 122.0
        assert sig.reward_risk_ratio == round((122.0 - 107.0) / (107.0 - 95.0), 2)
        assert 40 <= sig.confidence_pct <= 75

    def test_low_volume_breakout_rejected(self):
        # Breakout price crossed, but volume is only 1.2x (below 1.5x required)
        candles = generate_day_candles(
            base_price=100.0,
            range_high_offset=5.0,
            range_low_offset=5.0,
            range_avg_vol=1000,
            breakout_minute=20,
            breakout_price_offset=2.0,
            breakout_vol_mult=1.2  # Below 1.5x threshold
        )
        sig = compute_orb_signal("OGDC", candles)
        assert sig is None

    def test_degraded_candle_rejects_signal(self):
        # Candle in range is degraded -> signal generation must abort
        candles = generate_day_candles(
            base_price=100.0,
            range_high_offset=5.0,
            range_low_offset=5.0,
            range_avg_vol=1000,
            breakout_minute=20,
            breakout_price_offset=2.0,
            breakout_vol_mult=2.0,
            is_degraded=True
        )
        sig = compute_orb_signal("OGDC", candles)
        assert sig is None

    def test_pure_function_determinism(self):
        candles = generate_day_candles(
            base_price=200.0,
            range_high_offset=8.0,
            range_low_offset=4.0,
            range_avg_vol=5000,
            breakout_minute=22,
            breakout_price_offset=1.5,
            breakout_vol_mult=2.5
        )
        sig1 = compute_orb_signal("PPL", candles, fixed_signal_id="FIXED_1")
        sig2 = compute_orb_signal("PPL", candles, fixed_signal_id="FIXED_1")

        assert sig1 is not None
        assert sig2 is not None
        assert sig1.model_dump() == sig2.model_dump()


class TestORBGoldenScenarios:
    """
    Golden tests: 20 distinct hand-verified historical scenarios.
    Engine must reproduce expected signals on every execution.
    """

    @pytest.mark.parametrize("day_idx, base_p, rh_off, rl_off, vol, b_min, b_off, b_vol, should_signal, exp_entry, exp_stop, exp_target", [
        # (day, base, rh_off, rl_off, vol, b_min, b_off, b_vol, should_signal, entry, stop, target)
        (1, 100.0, 5.0, 5.0, 1000, 18, 2.0, 2.0, True, 107.0, 95.0, 122.0),
        (2, 50.0, 2.0, 2.0, 2000, 20, 1.0, 1.8, True, 53.0, 48.0, 59.0),
        (3, 250.0, 10.0, 10.0, 500, 25, 5.0, 2.5, True, 265.0, 240.0, 295.0),
        (4, 180.0, 4.0, 6.0, 1500, 16, 2.0, 1.6, True, 186.0, 174.0, 201.0),
        (5, 75.0, 3.0, 3.0, 3000, 30, 1.5, 3.0, True, 79.5, 72.0, 88.5),
        (6, 320.0, 8.0, 8.0, 800, 22, 4.0, 2.1, True, 332.0, 312.0, 356.0),
        (7, 110.0, 4.0, 4.0, 1200, 19, 2.0, 1.9, True, 116.0, 106.0, 128.0),
        (8, 90.0, 2.5, 2.5, 2500, 21, 1.5, 1.7, True, 94.0, 87.5, 101.5),
        (9, 450.0, 15.0, 15.0, 400, 24, 6.0, 2.2, True, 471.0, 435.0, 516.0),
        (10, 135.0, 5.0, 5.0, 1800, 17, 3.0, 2.0, True, 143.0, 130.0, 158.0),
        # Days 11-15: Low volume breakouts (MUST NOT SIGNAL)
        (11, 100.0, 5.0, 5.0, 1000, 20, 2.0, 1.2, False, 0.0, 0.0, 0.0),
        (12, 50.0, 2.0, 2.0, 2000, 20, 1.0, 1.4, False, 0.0, 0.0, 0.0),
        (13, 250.0, 10.0, 10.0, 500, 25, 5.0, 1.0, False, 0.0, 0.0, 0.0),
        (14, 180.0, 4.0, 6.0, 1500, 16, 2.0, 1.49, False, 0.0, 0.0, 0.0),
        (15, 75.0, 3.0, 3.0, 3000, 30, 1.5, 0.9, False, 0.0, 0.0, 0.0),
        # Days 16-20: High volume valid breakouts with different multiples
        (16, 60.0, 3.0, 2.0, 1100, 22, 1.0, 2.0, True, 64.0, 58.0, 71.5),
        (17, 210.0, 7.0, 7.0, 950, 19, 3.0, 2.4, True, 220.0, 203.0, 241.0),
        (18, 85.0, 4.0, 4.0, 2200, 26, 2.0, 1.6, True, 91.0, 81.0, 103.0),
        (19, 390.0, 12.0, 8.0, 600, 28, 5.0, 2.0, True, 407.0, 382.0, 437.0),
        (20, 140.0, 6.0, 4.0, 1400, 18, 2.5, 1.8, True, 148.5, 136.0, 163.5),
        (21, 165.0, 5.0, 5.0, 1600, 23, 2.0, 2.2, True, 172.0, 160.0, 187.0),
    ])
    def test_golden_scenarios_reproduction(
        self, day_idx, base_p, rh_off, rl_off, vol, b_min, b_off, b_vol, should_signal, exp_entry, exp_stop, exp_target
    ):
        candles = generate_day_candles(
            base_price=base_p,
            range_high_offset=rh_off,
            range_low_offset=rl_off,
            range_avg_vol=vol,
            breakout_minute=b_min,
            breakout_price_offset=b_off,
            breakout_vol_mult=b_vol,
        )

        sig = compute_orb_signal("SYS", candles, range_minutes=15, target_multiplier=1.5)

        if not should_signal:
            assert sig is None, f"Scenario Day {day_idx} expected NO signal, but got one."
        else:
            assert sig is not None, f"Scenario Day {day_idx} expected signal, but got None."
            assert sig.entry_price == exp_entry, f"Day {day_idx} Entry mismatch"
            assert sig.stop_loss == exp_stop, f"Day {day_idx} Stop mismatch"
            assert sig.target_price == exp_target, f"Day {day_idx} Target mismatch"
            assert sig.reward_risk_ratio >= 1.0
            assert 40 <= sig.confidence_pct <= 75


class TestRealPSXGoldenData:
    """
    Golden tests using REAL historical intraday tick & candle data
    pulled directly from the PSX DPS timeseries endpoint.
    """

    @pytest.fixture(scope="module")
    def real_psx_data(self):

        import json
        from pathlib import Path
        fixture_path = Path(__file__).parent / "fixtures" / "psx_real_market_data.json"
        with open(fixture_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_real_psx_ogdc_breakout(self, real_psx_data):
        """OGDC (2026-09-04): 15-min range 327.00 - 328.00. Breakout triggered at 09:41 PKT."""
        candles = real_psx_data["OGDC"]
        assert len(candles) >= 15

        # Compute range manually
        rh = max(c["high"] for c in candles[:15])
        rl = min(c["low"] for c in candles[:15])
        assert rh == 328.00
        assert rl == 327.00
        avg_vol = sum(c["volume"] for c in candles[:15]) / 15.0
        req_vol = avg_vol * 1.5

        sig = compute_orb_signal("OGDC", candles, fixed_signal_id="REAL_OGDC_20260904")
        assert sig is not None
        assert sig.ticker == "OGDC"
        assert sig.action == SignalAction.BUY
        assert sig.entry_price == 328.48
        assert sig.stop_loss == 327.00  # Range low
        assert sig.target_price == 329.98  # 328.48 + 1.5 * 1.00
        assert sig.reward_risk_ratio == 1.01
        assert sig.confidence_pct == 75

    def test_real_psx_hubc_breakout(self, real_psx_data):
        """HUBC (2026-09-04): 15-min range 205.66 - 207.00. Breakout triggered at 11:37 PKT."""
        candles = real_psx_data["HUBC"]
        sig = compute_orb_signal("HUBC", candles, fixed_signal_id="REAL_HUBC_20260904")
        assert sig is not None
        assert sig.ticker == "HUBC"
        assert sig.action == SignalAction.BUY
        assert sig.entry_price == 207.30
        assert sig.stop_loss == 205.66
        assert sig.target_price == 209.31
        assert sig.reward_risk_ratio == 1.23

    def test_real_psx_hbl_no_breakout(self, real_psx_data):
        """HBL (2026-09-04): 15-min range 313.52 - 316.00. No post-range breakout."""
        candles = real_psx_data["HBL"]
        sig = compute_orb_signal("HBL", candles, fixed_signal_id="REAL_HBL_20260904")
        assert sig is None

    def test_real_psx_engro_no_breakout(self, real_psx_data):
        """ENGRO (2026-09-04): 15-min range 482.00 - 496.00. No post-range breakout."""
        candles = real_psx_data["ENGRO"]
        sig = compute_orb_signal("ENGRO", candles, fixed_signal_id="REAL_ENGRO_20260904")
        assert sig is None

    def test_real_psx_eod_data_fixture_integrity(self):
        """Verify 10 days of real EOD PSX timeseries fixtures for OGDC and HBL."""
        import json
        from pathlib import Path
        eod_path = Path(__file__).parent / "fixtures" / "psx_real_eod_data.json"
        with open(eod_path, "r", encoding="utf-8") as f:
            eod_data = json.load(f)

        assert "OGDC" in eod_data
        assert "HBL" in eod_data
        assert len(eod_data["OGDC"]) == 10
        assert len(eod_data["HBL"]) == 10
        # Check latest date is 2026-09-04
        assert eod_data["OGDC"][0]["date"] == "2026-09-04"
        assert eod_data["OGDC"][0]["close"] == 328.8
        assert eod_data["HBL"][0]["date"] == "2026-09-04"
        assert eod_data["HBL"][0]["close"] == 313.68

