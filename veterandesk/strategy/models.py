"""
Data models for the strategy engine and trading signals.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(str, Enum):
    GENERATED = "GENERATED"
    APPROVED = "APPROVED"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class TradeSignal(BaseModel):
    """
    Standardized trading signal emitted by strategy engines.
    Guarantees all required fields, confidence bounds (40-75%),
    and reward:risk requirements.
    """
    signal_id: str
    ticker: str
    strategy: str = "ORB_v1.0"
    strategy_version: str = "1.0.0"
    action: SignalAction = SignalAction.BUY
    entry_price: float = Field(..., gt=0)
    stop_loss: float = Field(..., gt=0)
    target_price: float = Field(..., gt=0)
    reward_risk_ratio: float = Field(..., ge=1.0)
    position_size: int = Field(default=0, ge=0)
    confidence_pct: int = Field(..., ge=40, le=75, description="Confidence strictly between 40% and 75%")
    invalidation_reason: str
    data_status: str = Field(default="ok")
    status: SignalStatus = SignalStatus.GENERATED
    rejection_reason: Optional[str] = None
    created_at: datetime
    session_id: str

    @field_validator("data_status")
    @classmethod
    def validate_data_status(cls, v: str) -> str:
        if v != "ok":
            raise ValueError(f"Signals can only be created with data_status='ok', got '{v}'")
        return v

    @field_validator("stop_loss")
    @classmethod
    def validate_stop_loss(cls, v: float, info: Any) -> float:
        entry = info.data.get("entry_price")
        action = info.data.get("action")
        if entry is not None and action == SignalAction.BUY:
            if v >= entry:
                raise ValueError(f"For BUY signals, stop_loss ({v}) must be strictly less than entry_price ({entry})")
        return v

    @field_validator("target_price")
    @classmethod
    def validate_target_price(cls, v: float, info: Any) -> float:
        entry = info.data.get("entry_price")
        action = info.data.get("action")
        if entry is not None and action == SignalAction.BUY:
            if v <= entry:
                raise ValueError(f"For BUY signals, target_price ({v}) must be strictly greater than entry_price ({entry})")
        return v
