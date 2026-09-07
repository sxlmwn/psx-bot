"""
Candle Builder & Intraday Replay Module.

Builds 1-min, 5-min, 15-min, and daily OHLCV candles from raw ticks.
Supports deterministic replay to reconstruct candles from historical ticks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class Candle:
    ticker: str
    timeframe: str  # '1m', '5m', '15m', '1d'
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime
    data_status: str = "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "timeframe": self.timeframe,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timestamp": self.timestamp,
            "data_status": self.data_status,
        }


def bucket_timestamp(dt: datetime, minutes: int) -> datetime:
    """Bucket a datetime to the floor minute interval."""
    total_minutes = dt.minute
    bucketed_minute = (total_minutes // minutes) * minutes
    return dt.replace(minute=bucketed_minute, second=0, microsecond=0)


def build_candles_from_ticks(
    ticks: List[Dict[str, Any]],
    timeframe_minutes: int = 1,
    timeframe_label: str = "1m"
) -> List[Candle]:
    """
    Replay & build candles from raw tick records.

    Each tick dict must contain:
    - 'ticker': str
    - 'price': float
    - 'volume': int
    - 'psx_timestamp': datetime
    - 'data_status': 'ok' | 'degraded' (optional, defaults to 'ok')
    """
    if not ticks:
        return []

    # Sort ticks chronologically
    sorted_ticks = sorted(ticks, key=lambda t: t["psx_timestamp"])
    ticker = sorted_ticks[0]["ticker"]

    # Group ticks by bucket
    buckets: Dict[datetime, List[Dict[str, Any]]] = {}
    for t in sorted_ticks:
        ts = t["psx_timestamp"]
        bucket = bucket_timestamp(ts, timeframe_minutes)
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(t)

    candles: List[Candle] = []
    for bucket_ts in sorted(buckets.keys()):
        bucket_ticks = buckets[bucket_ts]
        open_price = float(bucket_ticks[0]["price"])
        close_price = float(bucket_ticks[-1]["price"])
        high_price = max(float(t["price"]) for t in bucket_ticks)
        low_price = min(float(t["price"]) for t in bucket_ticks)

        # Volume handling:
        # If ticks are monotonically increasing cumulative volumes, candle volume is the delta (vol_end - vol_start).
        # Otherwise (e.g. individual trade-level tick volumes from DPS timeseries), candle volume is the sum.
        vol_start = int(bucket_ticks[0]["volume"])
        vol_end = int(bucket_ticks[-1]["volume"])
        if len(bucket_ticks) > 1 and all(int(bucket_ticks[i]["volume"]) <= int(bucket_ticks[i+1]["volume"]) for i in range(len(bucket_ticks) - 1)) and vol_end > vol_start:
            candle_vol = vol_end - vol_start
        else:
            candle_vol = sum(int(t.get("volume", 0)) for t in bucket_ticks)

        # Status is degraded if any tick was degraded
        status = "ok"
        if any(t.get("data_status") == "degraded" for t in bucket_ticks):
            status = "degraded"

        candles.append(Candle(
            ticker=ticker,
            timeframe=timeframe_label,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=candle_vol,
            timestamp=bucket_ts,
            data_status=status
        ))

    return candles
