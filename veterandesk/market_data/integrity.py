"""
End-of-Day Candle Integrity Checker.

Verifies that 1-minute candle count matches expected session duration
and flags missing or degraded time intervals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import List, Tuple
from veterandesk.market_data.candle_builder import Candle


@dataclass(frozen=True)
class SessionIntegrityReport:
    expected_candles: int
    actual_candles: int
    missing_intervals: List[Tuple[datetime, datetime]]
    degraded_candles: int
    is_healthy: bool
    details: str


def check_session_integrity(
    candles_1m: List[Candle],
    session_start: datetime,
    session_end: datetime,
    tolerance_missing_pct: float = 2.0
) -> SessionIntegrityReport:
    """
    Check if 1-minute candles cover the continuous session without unexpected holes.
    """
    total_minutes = int((session_end - session_start).total_seconds() / 60)
    if total_minutes <= 0:
        total_minutes = 1

    candle_map = {c.timestamp: c for c in candles_1m}
    missing_intervals: List[Tuple[datetime, datetime]] = []
    degraded_count = sum(1 for c in candles_1m if c.data_status == "degraded")

    curr = session_start
    in_gap = False
    gap_start = curr

    while curr < session_end:
        if curr not in candle_map:
            if not in_gap:
                in_gap = True
                gap_start = curr
        else:
            if in_gap:
                in_gap = False
                missing_intervals.append((gap_start, curr))
        curr += timedelta(minutes=1)

    if in_gap:
        missing_intervals.append((gap_start, session_end))

    actual = len(candles_1m)
    missing_count = total_minutes - actual
    missing_pct = (missing_count / total_minutes) * 100.0 if total_minutes > 0 else 0.0
    is_healthy = missing_pct <= tolerance_missing_pct and degraded_count == 0

    report = SessionIntegrityReport(
        expected_candles=total_minutes,
        actual_candles=actual,
        missing_intervals=missing_intervals,
        degraded_candles=degraded_count,
        is_healthy=is_healthy,
        details=(
            f"Session minutes: {total_minutes}, Actual 1m candles: {actual} "
            f"({missing_pct:.1f}% missing, {degraded_count} degraded)"
        )
    )
    return report
