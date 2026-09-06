"""
Independent Mistake Detection and Audit Module.

Runs as an independent post-trade audit, completely decoupled from Risk Engine.
If Risk Engine allowed a trade that the Audit flags as a violation,
a CRITICAL alert is triggered immediately (indicating potential rule bypass).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import List, Optional
import uuid

from veterandesk.execution.paper_broker import DemoTrade
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.mistake_detector")


class MistakeSeverity(str, Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class DetectedMistake:
    id: str
    trade_id: Optional[str]
    rule_violated: str
    severity: MistakeSeverity
    details: str
    detected_at: datetime


class MistakeDetector:
    """
    Independent audit engine.
    """

    def __init__(
        self,
        max_risk_pct: float = 1.00,
        max_daily_trades: int = 3,
        entry_cutoff_pkt: time = time(15, 0, 0),
        force_close_pkt: time = time(15, 20, 0),
    ) -> None:
        self.max_risk_pct = max_risk_pct
        self.max_daily_trades = max_daily_trades
        self.entry_cutoff_pkt = entry_cutoff_pkt
        self.force_close_pkt = force_close_pkt
        self.audit_log: List[DetectedMistake] = []

    def audit_trade(
        self,
        trade: DemoTrade,
        account_balance_at_entry: float,
        session_trades_count: int,
        entry_time_pkt: time,
    ) -> List[DetectedMistake]:
        """
        Audit a trade against all non-negotiable discipline rules.
        """
        mistakes: List[DetectedMistake] = []
        ts = datetime.now(timezone.utc)

        # Audit 1: No Stop Loss
        if trade.stop_loss is None or trade.stop_loss <= 0:
            m = DetectedMistake(
                id=str(uuid.uuid4()),
                trade_id=trade.trade_id,
                rule_violated="NO_STOP_LOSS",
                severity=MistakeSeverity.CRITICAL,
                details=f"Trade {trade.trade_id} entered without a valid stop loss!",
                detected_at=ts,
            )
            mistakes.append(m)

        # Audit 2: Oversize / Risk Limit Exceeded
        if account_balance_at_entry > 0 and trade.stop_loss is not None:
            rupee_risk = trade.shares * (trade.filled_entry_price - trade.stop_loss)
            risk_pct = (rupee_risk / account_balance_at_entry) * 100.0
            if risk_pct > (self.max_risk_pct + 0.01):
                m = DetectedMistake(
                    id=str(uuid.uuid4()),
                    trade_id=trade.trade_id,
                    rule_violated="OVERSIZE_RISK_LIMIT_EXCEEDED",
                    severity=MistakeSeverity.CRITICAL,
                    details=f"Risk taken {risk_pct:.2f}% exceeds maximum limit {self.max_risk_pct:.2f}% (Rupee risk: PKR {rupee_risk:,.2f})",
                    detected_at=ts,
                )
                mistakes.append(m)

        # Audit 3: Late Entry After Cutoff
        if entry_time_pkt >= self.entry_cutoff_pkt:
            m = DetectedMistake(
                id=str(uuid.uuid4()),
                trade_id=trade.trade_id,
                rule_violated="LATE_ENTRY_AFTER_CUTOFF",
                severity=MistakeSeverity.CRITICAL,
                details=f"Trade entered at {entry_time_pkt.strftime('%H:%M:%S')} PKT, which is after {self.entry_cutoff_pkt.strftime('%H:%M:%S')} cutoff",
                detected_at=ts,
            )
            mistakes.append(m)

        # Audit 4: Trade Limit Breach
        if session_trades_count > self.max_daily_trades:
            m = DetectedMistake(
                id=str(uuid.uuid4()),
                trade_id=trade.trade_id,
                rule_violated="DAILY_TRADE_LIMIT_BREACH",
                severity=MistakeSeverity.CRITICAL,
                details=f"Trade executed when daily count ({session_trades_count}) exceeded cap ({self.max_daily_trades})",
                detected_at=ts,
            )
            mistakes.append(m)

        # Audit 5: Held Through Stop
        if trade.exit_price is not None and trade.stop_loss is not None:
            if trade.action.value == "BUY" and trade.exit_price < trade.stop_loss * 0.98:
                m = DetectedMistake(
                    id=str(uuid.uuid4()),
                    trade_id=trade.trade_id,
                    rule_violated="HELD_THROUGH_STOP",
                    severity=MistakeSeverity.CRITICAL,
                    details=f"Exit price {trade.exit_price:.2f} fell significantly below stop loss {trade.stop_loss:.2f}",
                    detected_at=ts,
                )
                mistakes.append(m)

        if mistakes:
            logger.critical(
                "mistakes_detected_by_audit",
                trade_id=trade.trade_id,
                count=len(mistakes),
                rules=[m.rule_violated for m in mistakes],
            )
            self.audit_log.extend(mistakes)
            self._persist_mistakes_to_db(mistakes)

            for m in mistakes:
                try:
                    from veterandesk.alerts.telegram import telegram_service
                    telegram_service.send_mistake_alert(
                        rule_violated=m.rule_violated,
                        severity=m.severity.value,
                        trade_id=m.trade_id,
                        details=m.details,
                        detected_at_str=m.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    )
                except Exception as ex:
                    logger.warning("telegram_mistake_alert_failed", error=str(ex), trade_id=m.trade_id)

                try:
                    from veterandesk.alerts.discord import discord_service
                    discord_service.send_mistake_alert(
                        rule_violated=m.rule_violated,
                        severity=m.severity.value,
                        trade_id=m.trade_id,
                        details=m.details,
                        detected_at_str=m.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    )
                except Exception as ex:
                    logger.warning("discord_mistake_alert_failed", error=str(ex), trade_id=m.trade_id)

        return mistakes

    def _persist_mistakes_to_db(self, mistakes: List[DetectedMistake]) -> None:
        """Persist detected discipline mistakes into Supabase PostgreSQL."""
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            rows = [
                {
                    "trade_id": m.trade_id,
                    "rule_violated": m.rule_violated,
                    "severity": m.severity.value,
                    "details": m.details,
                    "detected_at": m.detected_at.isoformat(),
                    "acknowledged": False,
                }
                for m in mistakes
            ]
            client.table("mistake_audit_log").insert(rows).execute()
            logger.info("mistakes_persisted_to_supabase", count=len(rows))
        except Exception as e:
            logger.warning("mistake_db_persistence_skipped", error=str(e))

    def verify_no_discrepancy(
        self,
        risk_engine_approved: bool,
        audit_mistakes: List[DetectedMistake]
    ) -> tuple[bool, Optional[str]]:
        """
        Check for discrepancy between Risk Engine approval and independent audit.
        """
        if risk_engine_approved and len(audit_mistakes) > 0:
            violations = ", ".join(m.rule_violated for m in audit_mistakes)
            alert_msg = (
                f"DISCREPANCY ALERT: Risk Engine approved a trade that independent audit "
                f"found violated rules: [{violations}]. Critical bypass suspected!"
            )
            logger.critical("risk_engine_audit_discrepancy", alert=alert_msg)
            try:
                from veterandesk.alerts.telegram import telegram_service
                telegram_service.send_mistake_alert(
                    rule_violated=f"AUDIT_DISCREPANCY_{violations}",
                    severity="CRITICAL",
                    trade_id=audit_mistakes[0].trade_id if audit_mistakes else None,
                    details=alert_msg,
                )
            except Exception as ex:
                logger.warning("telegram_discrepancy_alert_failed", error=str(ex))

            try:
                from veterandesk.alerts.discord import discord_service
                discord_service.send_mistake_alert(
                    rule_violated=f"AUDIT_DISCREPANCY_{violations}",
                    severity="CRITICAL",
                    trade_id=audit_mistakes[0].trade_id if audit_mistakes else None,
                    details=alert_msg,
                )
            except Exception as ex:
                logger.warning("discord_discrepancy_alert_failed", error=str(ex))
            return False, alert_msg

        return True, None
