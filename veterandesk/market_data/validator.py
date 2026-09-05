"""
Market Data Tick Sanity Validator.

Sanity checks:
1. Price must be strictly positive (price > 0).
2. Volume must be monotonic non-decreasing intraday.
3. Price jump > 10% vs last tick without volume confirmation is flagged as suspicious.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class TickValidationResult:
    is_valid: bool
    status: str  # 'ok', 'degraded', 'rejected'
    reason: str


class TickValidator:
    """
    Stateful validator tracking last observed tick per ticker to enforce
    monotonic volume and detect anomalous price jumps.
    """

    def __init__(self) -> None:
        # ticker -> (last_price, last_volume)
        self._last_ticks: Dict[str, Tuple[float, int]] = {}

    def validate_tick(
        self,
        ticker: str,
        price: float,
        volume: int,
        volume_delta_min_confirm: int = 1000
    ) -> TickValidationResult:
        """
        Validate incoming raw tick data.
        """
        sym = ticker.upper()

        # Rule 1: Price must be strictly positive
        if price <= 0:
            return TickValidationResult(
                is_valid=False,
                status="rejected",
                reason=f"Non-positive price detected for {sym}: {price}"
            )

        if volume < 0:
            return TickValidationResult(
                is_valid=False,
                status="rejected",
                reason=f"Negative volume detected for {sym}: {volume}"
            )

        if sym in self._last_ticks:
            last_price, last_volume = self._last_ticks[sym]

            # Rule 2: Volume must be monotonic non-decreasing intraday
            if volume < last_volume:
                return TickValidationResult(
                    is_valid=False,
                    status="rejected",
                    reason=(
                        f"Non-monotonic volume for {sym}: current volume ({volume:,}) "
                        f"< previous volume ({last_volume:,})"
                    )
                )

            # Rule 3: Price change > 10% without volume confirmation
            if last_price > 0:
                pct_change = abs((price - last_price) / last_price) * 100.0
                volume_delta = volume - last_volume

                if pct_change > 10.0 and volume_delta < volume_delta_min_confirm:
                    return TickValidationResult(
                        is_valid=False,
                        status="degraded",
                        reason=(
                            f"Suspicious {pct_change:.2f}% price jump in {sym} "
                            f"(from {last_price} to {price}) without volume confirmation (delta={volume_delta})"
                        )
                    )

        # Update last known state
        self._last_ticks[sym] = (price, volume)

        return TickValidationResult(
            is_valid=True,
            status="ok",
            reason="Tick sanity checks passed."
        )

    def reset(self) -> None:
        """Reset intraday state at market open."""
        self._last_ticks.clear()
