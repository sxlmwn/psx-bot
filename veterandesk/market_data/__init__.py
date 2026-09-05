"""Market Data package for VeteranDesk."""

from veterandesk.market_data.candle_builder import Candle, build_candles_from_ticks
from veterandesk.market_data.gap_detector import GapDetector, PollHealthState
from veterandesk.market_data.integrity import SessionIntegrityReport, check_session_integrity
from veterandesk.market_data.latency import LatencyAssessment, evaluate_latency
from veterandesk.market_data.scraper import PSXDpsScraper
from veterandesk.market_data.validator import TickValidationResult, TickValidator

__all__ = [
    "Candle",
    "build_candles_from_ticks",
    "GapDetector",
    "PollHealthState",
    "SessionIntegrityReport",
    "check_session_integrity",
    "LatencyAssessment",
    "evaluate_latency",
    "PSXDpsScraper",
    "TickValidationResult",
    "TickValidator",
]
