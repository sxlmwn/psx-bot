"""
Opening Range Breakout (ORB v1.0) Strategy Engine.

Non-negotiable Design Principles:
1. Strategy is a PURE DETERMINISTIC FUNCTION of candle data + parameters.
2. Same candle input ALWAYS produces the exact same signal output.
3. No ML, no randomness, no floating-point ambiguity.
4. Signals only generated when data status is 'ok', never on 'degraded'.
5. Hard maximum of 1 ORB trade per ticker per day.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from veterandesk.strategy.models import TradeSignal, SignalAction, SignalStatus


def compute_orb_signal(
    ticker: str,
    candles_1m: List[Dict[str, Any]],
    range_minutes: int = 15,
    volume_multiplier: float = 1.50,
    target_multiplier: float = 1.50,
    session_id: str = "default_session",
    fixed_signal_id: Optional[str] = None,
    notify: bool = False,
) -> Optional[TradeSignal]:
    """
    Pure deterministic ORB strategy calculation.

    Args:
        ticker: Symbol name (e.g. 'OGDC')
        candles_1m: List of 1-minute candle dicts sorted by timestamp ASC.
                    Each candle dict must have:
                    'timestamp': datetime (UTC or PKT)
                    'open': float
                    'high': float
                    'low': float
                    'close': float
                    'volume': int/float
                    'data_status': 'ok' | 'degraded'
        range_minutes: Opening range length in minutes (default 15)
        volume_multiplier: Minimum volume expansion factor (default 1.5x)
        target_multiplier: Target distance as multiple of range height (1.5x - 2.0x)
        session_id: Current system session ID
        fixed_signal_id: Optional deterministic ID for golden testing

    Returns:
        TradeSignal if a valid breakout occurs, else None.
    """
    if len(candles_1m) < range_minutes + 1:
        return None

    # Step 1: Extract opening range window
    range_candles = candles_1m[:range_minutes]

    # Validate data quality across the opening range
    for c in range_candles:
        if c.get("data_status", "ok") != "ok":
            return None

    range_high = max(float(c["high"]) for c in range_candles)
    range_low = min(float(c["low"]) for c in range_candles)
    range_height = range_high - range_low

    if range_height <= 0:
        return None

    total_range_volume = sum(float(c["volume"]) for c in range_candles)
    avg_range_volume = total_range_volume / range_minutes

    if avg_range_volume <= 0:
        return None

    min_required_volume = avg_range_volume * volume_multiplier

    # Step 2: Scan post-range candles sequentially for the FIRST breakout
    post_range_candles = candles_1m[range_minutes:]

    for candle in post_range_candles:
        # If data is degraded, do not emit signal
        if candle.get("data_status", "ok") != "ok":
            continue

        close_price = float(candle["close"])
        candle_vol = float(candle["volume"])

        # ORB Long Trigger: 1-min close above range high WITH volume >= 1.5x range average
        if close_price > range_high and candle_vol >= min_required_volume:
            entry_price = round(close_price, 2)
            stop_loss = round(range_low, 2)

            # Target = entry + target_multiplier * range_height
            raw_target = entry_price + (target_multiplier * range_height)
            target_price = round(raw_target, 2)

            risk_per_share = entry_price - stop_loss
            reward_per_share = target_price - entry_price

            if risk_per_share <= 0:
                continue

            rr_ratio = round(reward_per_share / risk_per_share, 2)
            if rr_ratio < 1.0:
                continue

            # Deterministic confidence between 40% and 75%
            # Base confidence 50% + bonus based on volume expansion (up to +25%)
            vol_ratio = candle_vol / avg_range_volume
            # Scaled: 1.5x -> 50%, 3.0x -> 75%
            extra_vol = max(0.0, vol_ratio - volume_multiplier)
            confidence = int(min(75, max(40, 50 + math.floor(extra_vol * 16.66))))

            candle_ts = candle["timestamp"]
            if isinstance(candle_ts, str):
                created_at = datetime.fromisoformat(candle_ts)
            else:
                created_at = candle_ts

            sig_id = fixed_signal_id or f"SIG_{ticker}_{int(created_at.timestamp())}_{uuid.uuid4().hex[:6]}"

            sig = TradeSignal(
                signal_id=sig_id,
                ticker=ticker.upper(),
                strategy="ORB_v1.0",
                strategy_version="1.0.0",
                action=SignalAction.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                reward_risk_ratio=rr_ratio,
                position_size=0,  # Size is computed strictly by Risk Engine
                confidence_pct=confidence,
                invalidation_reason=f"1-min close below range high ({range_high:.2f}) or 15:20 PKT cutoff",
                data_status="ok",
                status=SignalStatus.GENERATED,
                created_at=created_at,
                session_id=session_id
            )

            if notify:
                try:
                    from veterandesk.alerts.telegram import telegram_service
                    telegram_service.send_signal_alert(
                        signal=sig,
                        shares=100,  # Provisional size until risk engine evaluates
                        reason_lines=f"ORB breakout on {volume_multiplier}x volume expansion.\nInvalidation: {sig.invalidation_reason}",
                    )
                except Exception:
                    pass

                try:
                    from veterandesk.alerts.discord import discord_service
                    discord_service.send_signal_alert(
                        signal=sig,
                        shares=100,  # Provisional size until risk engine evaluates
                        reason_lines=f"ORB breakout on {volume_multiplier}x volume expansion.\nInvalidation: {sig.invalidation_reason}",
                    )
                except Exception:
                    pass

            return sig

    return None
