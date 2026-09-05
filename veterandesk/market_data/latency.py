"""
Market Data Latency Monitor.

Checks time elapsed between exchange timestamp and scraper receipt.
Alerts if latency exceeds threshold (90 seconds) and marks candles degraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Tuple


@dataclass(frozen=True)
class LatencyAssessment:
    latency_seconds: float
    is_acceptable: bool
    status: str  # 'ok' | 'degraded'
    alert_required: bool
    message: str


def evaluate_latency(
    psx_timestamp: datetime,
    scraped_at: datetime,
    max_latency_seconds: float = 90.0
) -> LatencyAssessment:
    """
    Compare scrape time vs PSX timestamp.
    """
    latency = abs((scraped_at - psx_timestamp).total_seconds())

    if latency > max_latency_seconds:
        msg = (
            f"DPS feed latency {latency:.1f}s exceeds threshold {max_latency_seconds:.0f}s. "
            "Marking affected data as 'degraded'."
        )
        return LatencyAssessment(
            latency_seconds=latency,
            is_acceptable=False,
            status="degraded",
            alert_required=True,
            message=msg
        )

    return LatencyAssessment(
        latency_seconds=latency,
        is_acceptable=True,
        status="ok",
        alert_required=False,
        message=f"Latency {latency:.1f}s is normal."
    )
