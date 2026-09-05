"""
Swing Thesis Generator and Schema Validator.

Enforces:
1. Strict schema validation (no thesis shown half-complete).
2. Confidence hard-capped at 75 in code.
3. R:R ratio computed strictly by deterministic engine from price levels.
4. Cites applied lessons from Lessons Memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
import uuid

from veterandesk.journal.lessons import LessonsMemory
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.swing_thesis")


class ThesisType(str, Enum):
    PULLBACK_VALUE = "PULLBACK_VALUE"
    BREAKOUT_MOMENTUM = "BREAKOUT_MOMENTUM"
    CATALYST_DIVIDEND = "CATALYST_DIVIDEND"
    EARNINGS_ACCELERATION = "EARNINGS_ACCELERATION"


@dataclass(frozen=True)
class SwingThesis:
    thesis_id: str
    ticker: str
    thesis_type: ThesisType
    investment_case: str
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    target_price: float
    reward_risk_ratio: float
    timeframe_days: int
    confidence_pct: int
    invalidation_criteria: str
    pre_planned_management: str
    applied_lessons: List[str]
    created_at: datetime


class SwingThesisGenerator:
    """
    Validates and generates swing thesis reports.
    """

    def __init__(self, lessons_memory: LessonsMemory) -> None:
        self.lessons_memory = lessons_memory

    def create_and_validate_thesis(
        self,
        ticker: str,
        thesis_type: ThesisType,
        investment_case: str,
        entry_zone_low: float,
        entry_zone_high: float,
        stop_loss: float,
        target_price: float,
        timeframe_days: int,
        raw_confidence_pct: int,
        invalidation_criteria: str,
        pre_planned_management: str,
        applied_lessons: Optional[List[str]] = None,
    ) -> SwingThesis:
        """
        Validates all fields strictly.
        Hard-caps confidence at 75%.
        Computes R:R deterministically.
        """
        sym = ticker.upper().strip()
        if not sym:
            raise ValueError("Ticker is mandatory")

        if not investment_case or len(investment_case.strip()) < 10:
            raise ValueError("Investment case must be articulated clearly (min 10 chars)")

        if entry_zone_low <= 0 or entry_zone_high < entry_zone_low:
            raise ValueError(f"Invalid entry zone: [{entry_zone_low}, {entry_zone_high}]")

        if stop_loss <= 0 or stop_loss >= entry_zone_low:
            raise ValueError(f"Stop loss ({stop_loss}) must be strictly below entry zone low ({entry_zone_low})")

        if target_price <= entry_zone_high:
            raise ValueError(f"Target price ({target_price}) must be strictly above entry zone high ({entry_zone_high})")

        if timeframe_days <= 0:
            raise ValueError("Timeframe in days must be positive")

        if not invalidation_criteria.strip():
            raise ValueError("Invalidation criteria must be specified")

        if not pre_planned_management.strip():
            raise ValueError("Pre-planned trade management must be specified")

        # Deterministic R:R computed using entry midpoint
        entry_mid = (entry_zone_low + entry_zone_high) / 2.0
        risk = entry_mid - stop_loss
        reward = target_price - entry_mid
        rr_ratio = round(reward / risk, 2)

        if rr_ratio < 1.50:
            raise ValueError(f"Swing thesis reward:risk ({rr_ratio:.2f}) does not meet minimum 1.50 requirement")

        # Hard-cap confidence strictly at 75% in code
        capped_confidence = min(75, max(40, int(raw_confidence_pct)))

        # Lessons check: must cite at least one lesson if active lessons exist
        active_lessons = self.lessons_memory.get_lessons_for_ticker(sym)
        cited: List[str] = applied_lessons or []
        if active_lessons and not cited:
            # Auto-link top active lesson and persist citation
            top_lesson = active_lessons[0]
            self.lessons_memory.cite_lesson(top_lesson)
            cited.append(f"[{top_lesson.category}] {top_lesson.lesson_text}")

        thesis = SwingThesis(
            thesis_id=f"THESIS_{sym}_{uuid.uuid4().hex[:6]}",
            ticker=sym,
            thesis_type=thesis_type,
            investment_case=investment_case.strip(),
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            stop_loss=stop_loss,
            target_price=target_price,
            reward_risk_ratio=rr_ratio,
            timeframe_days=timeframe_days,
            confidence_pct=capped_confidence,
            invalidation_criteria=invalidation_criteria.strip(),
            pre_planned_management=pre_planned_management.strip(),
            applied_lessons=cited,
            created_at=datetime.now(timezone.utc),
        )

        logger.info(
            "swing_thesis_validated",
            ticker=sym,
            thesis_id=thesis.thesis_id,
            confidence=capped_confidence,
            rr_ratio=rr_ratio,
        )
        return thesis
