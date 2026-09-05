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
from veterandesk.strategy.models import TradeSignal, SignalAction, SignalStatus

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


@app.get("/trades", tags=["Execution"])
def get_all_supabase_trades() -> Dict[str, Any]:
    """Retrieve trades stored in live Supabase PostgreSQL database."""
    try:
        from veterandesk.database.session import db_manager
        client = db_manager.get_client()
        res = client.table("trades").select("*").order("opened_at", desc=True).execute()
        return {
            "source": "Supabase (PostgreSQL)",
            "count": len(res.data),
            "trades": res.data
        }
    except Exception as e:
        return {
            "source": "Local Fallback",
            "error": str(e),
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "ticker": t.ticker,
                    "shares": t.shares,
                    "entry_price": t.filled_entry_price,
                    "stop_loss": t.stop_loss,
                    "target_price": t.target_price,
                    "status": t.status.value,
                    "opened_at": t.opened_at.isoformat(),
                }
                for t in broker.open_trades.values()
            ]
        }


class TestTradeRequest(BaseModel):
    ticker: str = "OGDC"
    shares: int = 100
    entry_price: float = 328.48
    stop_loss: float = 327.00
    target_price: float = 329.98


@app.post("/trades/test", tags=["Execution"])
def create_test_trade(req: Optional[TestTradeRequest] = None) -> Dict[str, Any]:
    """Create a verified test trade and persist it directly to live Supabase PostgreSQL."""
    params = req or TestTradeRequest()
    sig = TradeSignal(
        signal_id=f"SIG_{params.ticker}_TEST",
        ticker=params.ticker,
        strategy="ORB_v1.0",
        strategy_version="1.0.0",
        action=SignalAction.BUY,
        entry_price=params.entry_price,
        stop_loss=params.stop_loss,
        target_price=params.target_price,
        reward_risk_ratio=round((params.target_price - params.entry_price) / (params.entry_price - params.stop_loss), 2),
        position_size=params.shares,
        confidence_pct=75,
        invalidation_reason="Test trade execution",
        data_status="ok",
        status=SignalStatus.GENERATED,
        created_at=datetime.now(timezone.utc),
        session_id="test_session"
    )
    trade = broker.execute_buy(
        signal=sig,
        scraped_price=params.entry_price,
        shares=params.shares
    )

    return {
        "status": "SUCCESS",
        "message": "Test trade executed and persisted to live Supabase PostgreSQL",
        "trade": {
            "trade_id": trade.trade_id,
            "ticker": trade.ticker,
            "shares": trade.shares,
            "entry_price": trade.filled_entry_price,
            "stop_loss": trade.stop_loss,
            "target_price": trade.target_price,
            "slippage_pct": trade.slippage_pct,
            "status": trade.status.value,
            "opened_at": trade.opened_at.isoformat(),
        }
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
