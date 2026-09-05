"""
Market Data Gap and Outage Detector.

Monitors polling health:
- 2 consecutive failures -> Issue warning alert
- 5 consecutive failures -> Trigger trade freeze ("data unreliable - no new trades")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from veterandesk.logging import get_logger

logger = get_logger("veterandesk.gap_detector")


@dataclass(frozen=True)
class PollHealthState:
    consecutive_failures: int
    is_halt_triggered: bool
    alert_message: Optional[str]


class GapDetector:
    def __init__(
        self,
        alert_threshold: int = 2,
        halt_threshold: int = 5
    ) -> None:
        self.alert_threshold = alert_threshold
        self.halt_threshold = halt_threshold
        self.consecutive_failures = 0
        self.is_halted = False

    def record_success(self) -> PollHealthState:
        """Record successful poll and clear failure streak."""
        was_halted = self.is_halted
        self.consecutive_failures = 0
        self.is_halted = False

        msg = None
        if was_halted:
            msg = "Market data poll restored. Resuming normal operations."
            logger.info("poll_restored", message=msg)

        return PollHealthState(
            consecutive_failures=0,
            is_halt_triggered=False,
            alert_message=msg
        )

    def record_failure(self, error_details: str) -> PollHealthState:
        """Record a poll failure and evaluate alert/halt triggers."""
        self.consecutive_failures += 1
        msg: Optional[str] = None

        if self.consecutive_failures >= self.halt_threshold:
            self.is_halted = True
            msg = (
                f"CRITICAL: {self.consecutive_failures} consecutive DPS poll failures! "
                "Data unreliable — signal generation halted and new trades frozen."
            )
            logger.critical("market_data_halt_triggered", failures=self.consecutive_failures, error=error_details)
        elif self.consecutive_failures >= self.alert_threshold:
            msg = (
                f"WARNING: {self.consecutive_failures} consecutive DPS poll failures. "
                "Monitoring feed closely."
            )
            logger.warning("market_data_poll_warning", failures=self.consecutive_failures, error=error_details)

        return PollHealthState(
            consecutive_failures=self.consecutive_failures,
            is_halt_triggered=self.is_halted,
            alert_message=msg
        )
