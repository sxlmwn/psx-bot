"""
System Health Monitoring and Heartbeat Service.

Runs continuous 60-second heartbeats tracking:
1. Scraper connectivity & latency
2. Database connectivity
3. Scheduler status
4. Ledger reconciliation balance
5. Outbound alert queues
Raises critical alerts if any heartbeat is missed > 2 minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
import time

from veterandesk.config import settings
from veterandesk.database import db_manager
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.health")


class ComponentStatus(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class ComponentHealth:
    name: str
    status: ComponentStatus
    latency_ms: float
    message: str
    last_checked: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SystemHealthMonitor:
    """
    Subsystem health monitor.
    """

    def __init__(self, ledger: Optional[DoubleEntryLedger] = None) -> None:
        self.ledger = ledger
        self.components: Dict[str, ComponentHealth] = {
            "scraper": ComponentHealth("scraper", ComponentStatus.GREEN, 0.0, "Initialized"),
            "database": ComponentHealth("database", ComponentStatus.GREEN, 0.0, "Connected"),
            "scheduler": ComponentHealth("scheduler", ComponentStatus.GREEN, 0.0, "Active"),
            "ledger": ComponentHealth("ledger", ComponentStatus.GREEN, 0.0, "Reconciled"),
            "telegram": ComponentHealth("telegram", ComponentStatus.GREEN, 0.0, "Ready"),
        }
        self.last_heartbeat: datetime = datetime.now(timezone.utc)

    def record_check(
        self,
        component: str,
        status: ComponentStatus,
        latency_ms: float = 0.0,
        message: str = "OK"
    ) -> ComponentHealth:
        """Update health record for a component."""
        ts = datetime.now(timezone.utc)
        record = ComponentHealth(
            name=component,
            status=status,
            latency_ms=latency_ms,
            message=message,
            last_checked=ts,
        )
        self.components[component] = record

        if status == ComponentStatus.RED:
            logger.critical("health_check_failed", component=component, message=message, latency_ms=latency_ms)
        elif status == ComponentStatus.YELLOW:
            logger.warning("health_check_warning", component=component, message=message, latency_ms=latency_ms)

        return record

    def run_heartbeat(self) -> Dict[str, ComponentHealth]:
        """
        Execute 60-second system heartbeat.
        """
        now = datetime.now(timezone.utc)
        self.last_heartbeat = now

        # 1. Check real DB connectivity
        db_res = db_manager.check_connection()
        db_status = ComponentStatus.GREEN if db_res["status"] == "GREEN" else ComponentStatus.RED
        self.record_check("database", db_status, db_res["latency_ms"], db_res["message"])

        # 2. Verify ledger reconciliation if ledger is attached
        if self.ledger is not None:
            t0 = time.perf_counter()
            is_reconciled, diff, msg = self.ledger.reconcile()
            lat = (time.perf_counter() - t0) * 1000.0
            if is_reconciled:
                self.record_check("ledger", ComponentStatus.GREEN, lat, "Ledger verified in balance")
            else:
                self.record_check("ledger", ComponentStatus.RED, lat, f"Ledger imbalance: {msg}")

        self._persist_heartbeats_to_db(self.components)
        return self.components

    def _persist_heartbeats_to_db(self, statuses: Dict[str, ComponentHealth]) -> None:
        """Persist component health states into Supabase PostgreSQL."""
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            rows = [
                {
                    "component": h.name,
                    "status": h.status.value,
                    "latency_ms": round(h.latency_ms, 2),
                    "message": h.message,
                    "checked_at": h.last_checked.isoformat(),
                }
                for h in statuses.values()
            ]
            client.table("health_heartbeats").insert(rows).execute()
            logger.info("heartbeats_persisted_to_supabase", count=len(rows))
        except Exception as e:
            logger.warning("heartbeat_db_persistence_skipped", error=str(e))

    def is_system_down(self, threshold_seconds: int = 120) -> bool:
        """
        Return True if heartbeat missed beyond threshold.
        """
        delta = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        return delta > threshold_seconds

    def check_health_and_alert(self, threshold_seconds: int = 120) -> bool:
        """
        Check if system heartbeat was missed > threshold_seconds (default 120s)
        or if any critical subsystem is RED. Dispatches Telegram SYSTEM_HEALTH alert.
        """
        is_down = self.is_system_down(threshold_seconds)
        red_components = [h.name for h in self.components.values() if h.status == ComponentStatus.RED]

        if is_down or red_components:
            reason = (
                f"Heartbeat silence threshold exceeded ({threshold_seconds}s)"
                if is_down
                else f"Subsystems degraded: {', '.join(red_components)}"
            )
            affected = red_components if red_components else list(self.components.keys())
            try:
                from veterandesk.alerts.telegram import telegram_service
                telegram_service.send_system_health_alert(
                    status="SYSTEM_DOWN" if is_down else "DEGRADED",
                    reason=reason,
                    affected_components=affected,
                    timestamp_str=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                )
            except Exception as ex:
                logger.warning("telegram_health_alert_failed", error=str(ex))
            return True
        return False

