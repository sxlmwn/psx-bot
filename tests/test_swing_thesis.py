"""
Tests for Swing Thesis Generator and Validation.
"""

import pytest
from veterandesk.journal.lessons import LessonsMemory
from veterandesk.strategy.swing_thesis import SwingThesisGenerator, ThesisType


class TestSwingThesis:
    def test_swing_thesis_validation_and_rr_calc(self):
        lessons_mem = LessonsMemory()
        lessons_mem.add_lesson("VALUATION", "Do not chase stretched PE tickers.")
        gen = SwingThesisGenerator(lessons_memory=lessons_mem)

        # Valid thesis: Entry [100, 104], Mid = 102. Stop = 96 (risk = 6). Target = 114 (reward = 12). R:R = 2.0
        thesis = gen.create_and_validate_thesis(
            ticker="ENGRO",
            thesis_type=ThesisType.PULLBACK_VALUE,
            investment_case="High dividend yield with stable fertilizer margins and strong dollar revenues.",
            entry_zone_low=100.0,
            entry_zone_high=104.0,
            stop_loss=96.0,
            target_price=114.0,
            timeframe_days=15,
            raw_confidence_pct=85,  # Above 75% -> Must be hard-capped
            invalidation_criteria="Weekly close below 95 PKR or dividend cut announcement.",
            pre_planned_management="Trim 50% at 108 PKR, trail stop to breakeven.",
        )

        assert thesis.ticker == "ENGRO"
        assert thesis.reward_risk_ratio == 2.0
        # Non-negotiable requirement: Confidence strictly hard-capped at 75% in code
        assert thesis.confidence_pct == 75
        assert len(thesis.applied_lessons) == 1
        assert "VALUATION" in thesis.applied_lessons[0]

    def test_swing_thesis_rejects_insufficient_rr(self):
        lessons_mem = LessonsMemory()
        gen = SwingThesisGenerator(lessons_memory=lessons_mem)

        # Low R:R (< 1.50) -> Rejected
        with pytest.raises(ValueError, match="does not meet minimum 1.50 requirement"):
            gen.create_and_validate_thesis(
                ticker="OGDC",
                thesis_type=ThesisType.BREAKOUT_MOMENTUM,
                investment_case="Circular debt resolution rally underway.",
                entry_zone_low=100.0,
                entry_zone_high=100.0,
                stop_loss=95.0,  # Risk = 5
                target_price=106.0,  # Reward = 6 -> R:R = 1.20 < 1.50
                timeframe_days=10,
                raw_confidence_pct=60,
                invalidation_criteria="Close below 95",
                pre_planned_management="Trail stop",
            )
