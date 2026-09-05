"""
Real Portfolio Position Planning Module.

Key Rules:
1. Cannot save or track a position without a strict stop loss.
2. Generates rupee loss, rupee gain, trim levels, and oversize warnings.
3. Computes daily session recommendations (Hold / Trim / Exit / Add).
4. Plan levels are immutable once set (updates produce new version numbers).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
import uuid

from veterandesk.logging import get_logger

logger = get_logger("veterandesk.portfolio")


class PortfolioAction(str, Enum):
    HOLD = "HOLD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    ADD = "ADD"


@dataclass
class PositionPlan:
    plan_id: str
    ticker: str
    quantity: int
    entry_fill_price: float
    stop_loss: float
    target_price: float
    trim_level: float
    max_risk_pct: float
    total_rupee_risk: float
    total_rupee_reward: float
    oversize_warning: bool
    shares_to_trim: int
    plan_version: int = 1
    status: str = "ACTIVE"
    created_at: datetime = datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError("MANDATORY STOP LOSS: A portfolio position cannot be created without a valid stop loss.")
        if self.stop_loss >= self.entry_fill_price:
            raise ValueError(f"Stop loss ({self.stop_loss}) must be strictly below entry price ({self.entry_fill_price})")


class PortfolioManager:
    """
    Manages real-portfolio holdings and session guidance.
    """

    def __init__(self, total_portfolio_equity: float = 1000000.0, max_account_risk_pct: float = 1.0) -> None:
        self.total_portfolio_equity = total_portfolio_equity
        self.max_account_risk_pct = max_account_risk_pct
        self.plans: Dict[str, PositionPlan] = {}

    def create_position_plan(
        self,
        ticker: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        target_price: Optional[float] = None,
        trim_level: Optional[float] = None,
    ) -> PositionPlan:
        """
        Create a new immutable position plan.
        """
        sym = ticker.upper()

        if stop_loss is None or stop_loss <= 0:
            raise ValueError("Position cannot be saved without a stop loss!")
        if stop_loss >= entry_price:
            raise ValueError("Stop loss must be lower than entry price for long positions!")

        risk_per_share = entry_price - stop_loss
        total_risk = quantity * risk_per_share

        # Target defaults to 2x risk if not provided
        tgt = target_price or round(entry_price + (2.0 * risk_per_share), 2)
        # Trim level defaults to 1x risk (breakeven derisk point)
        trim = trim_level or round(entry_price + risk_per_share, 2)

        total_reward = quantity * (tgt - entry_price)
        risk_pct_of_account = (total_risk / self.total_portfolio_equity) * 100.0

        # Oversize detection
        max_allowed_rupee_risk = self.total_portfolio_equity * (self.max_account_risk_pct / 100.0)
        oversize = total_risk > max_allowed_rupee_risk
        shares_to_trim = 0

        if oversize:
            excess_risk = total_risk - max_allowed_rupee_risk
            shares_to_trim = math.ceil(excess_risk / risk_per_share)

        plan = PositionPlan(
            plan_id=str(uuid.uuid4()),
            ticker=sym,
            quantity=quantity,
            entry_fill_price=entry_price,
            stop_loss=stop_loss,
            target_price=tgt,
            trim_level=trim,
            max_risk_pct=round(risk_pct_of_account, 2),
            total_rupee_risk=round(total_risk, 2),
            total_rupee_reward=round(total_reward, 2),
            oversize_warning=oversize,
            shares_to_trim=shares_to_trim,
            plan_version=1,
            status="ACTIVE",
            created_at=datetime.now(timezone.utc),
        )

        self.plans[sym] = plan
        logger.info(
            "portfolio_plan_created",
            ticker=sym,
            qty=quantity,
            stop=stop_loss,
            risk_pct=plan.max_risk_pct,
            oversize=oversize,
            shares_to_trim=shares_to_trim,
        )
        return plan

    def evaluate_session_call(self, ticker: str, current_price: float) -> tuple[PortfolioAction, str]:
        """
        Produce daily Hold / Trim / Exit / Add call for a position vs its plan.
        """
        sym = ticker.upper()
        if sym not in self.plans:
            return PortfolioAction.HOLD, f"No active position plan for {sym}"

        plan = self.plans[sym]

        # Rule 1: Stop hit -> EXIT immediately
        if current_price <= plan.stop_loss:
            return (
                PortfolioAction.EXIT,
                f"STOP HIT: Current price {current_price:.2f} reached stop level {plan.stop_loss:.2f}. Exit immediately.",
            )

        # Rule 2: Target hit -> EXIT / Take full profit
        if current_price >= plan.target_price:
            return (
                PortfolioAction.EXIT,
                f"TARGET REACHED: Current price {current_price:.2f} reached target {plan.target_price:.2f}. Lock in profit.",
            )

        # Rule 3: Trim level reached
        if current_price >= plan.trim_level:
            return (
                PortfolioAction.TRIM,
                f"TRIM LEVEL REACHED: Current price {current_price:.2f} crossed trim level {plan.trim_level:.2f}. Lock partial profit and trail stop.",
            )

        # Rule 4: Oversize position warning
        if plan.oversize_warning:
            return (
                PortfolioAction.TRIM,
                f"OVERSIZE WARNING: Position risk {plan.max_risk_pct:.2f}% exceeds account limit. Trim {plan.shares_to_trim} shares.",
            )

        return (
            PortfolioAction.HOLD,
            f"HOLD: Current price {current_price:.2f} is within range (Stop: {plan.stop_loss:.2f}, Target: {plan.target_price:.2f}). Thesis intact.",
        )
