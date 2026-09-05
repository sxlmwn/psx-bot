"""
FastAPI Application Backend for VeteranDesk.

Provides RESTful interfaces for:
- System health and component status
- Real-time trading signals
- Risk assessments and position sizing
- Demo ledger, balance, and performance metrics
- Real-portfolio position planning
- Post-mortem journals and active lessons
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

from veterandesk.config import settings
from veterandesk.execution.graduation import compute_performance_metrics
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import PaperBroker
from veterandesk.health.monitor import SystemHealthMonitor
from veterandesk.journal.lessons import LessonsMemory
from veterandesk.journal.post_mortem import PostMortemEngine
from veterandesk.portfolio.manager import PortfolioManager
from veterandesk.risk.engine import risk_engine
from veterandesk.strategy.models import TradeSignal

# Initialize core services
app = FastAPI(
    title="VeteranDesk PSX Trading Engine API",
    version=settings.app_version,
    description="Deterministic trading, risk discipline, and paper ledger execution for PSX equities."
)

ledger = DoubleEntryLedger(starting_balance_pkr=settings.starting_balance_pkr)
broker = PaperBroker(ledger=ledger)
portfolio_mgr = PortfolioManager()
lessons_mem = LessonsMemory()
post_mortem_engine = PostMortemEngine(lessons_memory=lessons_mem)
health_monitor = SystemHealthMonitor(ledger=ledger)


from veterandesk.database import db_manager


@app.get("/health", tags=["System"])
def get_health() -> Dict[str, Any]:
    """Get system health heartbeats and overall status."""
    statuses = health_monitor.run_heartbeat()
    is_down = health_monitor.is_system_down()
    db_check = db_manager.check_connection()
    return {
        "status": "RED" if is_down else "GREEN",
        "is_system_down": is_down,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database_connection": db_check,
        "components": {k: {"status": v.status.value, "latency_ms": v.latency_ms, "message": v.message} for k, v in statuses.items()}
    }


@app.get("/metrics", tags=["Performance"])
def get_performance_metrics() -> Dict[str, Any]:
    """Compute and retrieve official demo performance and graduation metrics."""
    metrics = compute_performance_metrics(
        closed_trades=broker.closed_trades,
        starting_balance=settings.starting_balance_pkr,
        recent_violations_count=0
    )
    return {
        "cash_balance": ledger.cash_balance,
        "equity_holdings": ledger.equity_holdings_value,
        "total_realized_pnl": ledger.realized_pnl,
        "metrics": metrics.__dict__,
    }


@app.get("/trades/open", tags=["Execution"])
def get_open_trades() -> List[Dict[str, Any]]:
    """List current open demo positions."""
    return [
        {
            "trade_id": t.trade_id,
            "ticker": t.ticker,
            "shares": t.shares,
            "entry_price": t.filled_entry_price,
            "stop_loss": t.stop_loss,
            "target_price": t.target_price,
            "opened_at": t.opened_at.isoformat(),
        }
        for t in broker.open_trades.values()
    ]


@app.get("/trades/closed", tags=["Execution"])
def get_closed_trades() -> List[Dict[str, Any]]:
    """List history of closed demo trades."""
    return [
        {
            "trade_id": t.trade_id,
            "ticker": t.ticker,
            "shares": t.shares,
            "entry_price": t.filled_entry_price,
            "exit_price": t.filled_exit_price,
            "exit_reason": t.exit_reason.value if t.exit_reason else None,
            "gross_pnl": t.gross_pnl,
            "fees_paid": t.entry_fees + t.exit_fees,
            "net_pnl": t.net_pnl,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in broker.closed_trades
    ]


@app.get("/journal", tags=["Journal"])
def get_journal() -> List[Dict[str, Any]]:
    """Retrieve all completed and pending trade post-mortems."""
    return [
        {
            "trade_id": r.trade_id,
            "ticker": r.ticker,
            "verdict": r.verdict.value if r.verdict else "PENDING",
            "analysis": r.post_mortem_analysis,
            "transferable_lesson": r.transferable_lesson,
            "status": r.status.value,
        }
        for r in post_mortem_engine.completed_journal.values()
    ]


@app.get("/lessons", tags=["Journal"])
def get_active_lessons() -> List[Dict[str, Any]]:
    """Retrieve all active transferable lessons."""
    return [
        {
            "id": l.id,
            "category": l.category,
            "text": l.lesson_text,
            "times_cited": l.times_cited,
            "is_active": l.is_active,
        }
        for l in lessons_mem.get_active_lessons()
    ]
