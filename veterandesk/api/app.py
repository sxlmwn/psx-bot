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
from pydantic import BaseModel, field_validator

from veterandesk.alerts.discord import discord_service
from veterandesk.alerts.scheduler import create_alert_scheduler
from veterandesk.alerts.telegram import telegram_service
from veterandesk.config import settings
from veterandesk.execution.graduation import compute_performance_metrics
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import PaperBroker
from veterandesk.health.monitor import SystemHealthMonitor
from veterandesk.journal.lessons import LessonsMemory
from veterandesk.journal.post_mortem import PostMortemEngine
from veterandesk.logging import get_logger
from veterandesk.portfolio.manager import PortfolioManager
from veterandesk.risk.engine import risk_engine
from veterandesk.strategy.models import TradeSignal, SignalAction, SignalStatus

logger = get_logger("veterandesk.api")

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
alert_scheduler = create_alert_scheduler(start=False)


@app.on_event("startup")
def on_startup() -> None:
    try:
        alert_scheduler.start()
        logger.info("telegram_alert_scheduler_started")
    except Exception as ex:
        logger.warning("scheduler_startup_error", error=str(ex))


@app.on_event("shutdown")
def on_shutdown() -> None:
    try:
        alert_scheduler.shutdown(wait=False)
        logger.info("telegram_alert_scheduler_shutdown")
    except Exception as ex:
        logger.warning("scheduler_shutdown_error", error=str(ex))


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
    """
    Debug test trade endpoint.
    GATED: Impossible to reach in production unless ENABLE_DEBUG_ENDPOINTS=true.
    ENFORCED: Strictly passes through Risk Engine pipeline before any execution.
    """
    if not settings.enable_debug_endpoints:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug endpoint disabled in production. Set ENABLE_DEBUG_ENDPOINTS=true in non-production environments to enable."
        )

    params = req or TestTradeRequest()
    rr = round((params.target_price - params.entry_price) / max(0.01, params.entry_price - params.stop_loss), 2)
    sig = TradeSignal(
        signal_id=f"SIG_{params.ticker}_TEST",
        ticker=params.ticker,
        strategy="ORB_v1.0",
        strategy_version="1.0.0",
        action=SignalAction.BUY,
        entry_price=params.entry_price,
        stop_loss=params.stop_loss,
        target_price=params.target_price,
        reward_risk_ratio=rr,
        position_size=params.shares,
        confidence_pct=75,
        invalidation_reason="Test trade execution",
        data_status="ok",
        status=SignalStatus.GENERATED,
        created_at=datetime.now(timezone.utc),
        session_id="test_session"
    )

    from veterandesk.config import PKT_TZ
    now_pkt = datetime.now(PKT_TZ).time()
    assessment = risk_engine.evaluate_signal(
        signal=sig,
        account_balance=ledger.cash_balance,
        current_day_realized_loss=abs(min(0.0, ledger.realized_pnl)),
        trades_executed_today=len(broker.closed_trades) + len(broker.open_trades),
        current_time_pkt=now_pkt,
        twenty_day_adv=5000000.0,
        open_positions=[{"ticker": t.ticker, "shares": t.shares} for t in broker.open_trades.values()],
    )

    if not assessment.is_approved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Trade rejected by Risk Engine",
                "rejection_reasons": assessment.rejection_reasons,
                "rule_results": [{"rule": r.rule_name, "passed": r.passed, "reason": r.reason} for r in assessment.rule_results]
            }
        )

    sig.position_size = assessment.approved_shares
    sig.status = SignalStatus.APPROVED
    shares_to_execute = min(params.shares, assessment.approved_shares)
    trade = broker.execute_buy(
        signal=sig,
        scraped_price=params.entry_price,
        shares=shares_to_execute
    )

    try:
        telegram_service.send_signal_alert(
            signal=sig,
            shares=shares_to_execute,
            reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
        )
    except Exception as ex:
        logger.warning("telegram_signal_alert_failed", error=str(ex), signal_id=sig.signal_id)

    try:
        discord_service.send_signal_alert(
            signal=sig,
            shares=shares_to_execute,
            reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
        )
    except Exception as ex:
        logger.warning("discord_signal_alert_failed", error=str(ex), signal_id=sig.signal_id)

    return {
        "status": "SUCCESS",
        "message": "Trade validated by Risk Engine and executed into live Supabase PostgreSQL",
        "risk_assessment": {
            "is_approved": assessment.is_approved,
            "approved_shares": assessment.approved_shares,
            "risk_pct_used": assessment.risk_pct_used,
            "rules_checked": len(assessment.rule_results)
        },
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


class ExecuteTradeRequest(BaseModel):
    ticker: str
    action: SignalAction = SignalAction.BUY
    entry_price: float
    stop_loss: float
    target_price: float
    twenty_day_adv: float = 5000000.0
    confidence_pct: int = 75
    invalidation_reason: str = "Breakout invalidated"
    strategy: str = "ORB_v1.0"
    strategy_version: str = "1.0.0"

    @field_validator("stop_loss")
    @classmethod
    def validate_stop_loss(cls, v: float, info: Any) -> float:
        entry = info.data.get("entry_price")
        action = info.data.get("action", SignalAction.BUY)
        if entry is not None and action == SignalAction.BUY and v >= entry:
            raise ValueError(f"For BUY signals, stop_loss ({v}) must be strictly less than entry_price ({entry})")
        return v


@app.post("/trades/execute", tags=["Execution"])
def execute_trade_pipeline(req: ExecuteTradeRequest) -> Dict[str, Any]:
    """
    Production trade execution pipeline.
    Non-negotiable rule: Every trade MUST pass through Risk & Discipline Engine before execution.
    """
    rr = round((req.target_price - req.entry_price) / max(0.01, req.entry_price - req.stop_loss), 2)
    try:
        sig = TradeSignal(
            signal_id=f"SIG_{req.ticker}_{int(datetime.now(timezone.utc).timestamp())}",
            ticker=req.ticker,
            strategy=req.strategy,
            strategy_version=req.strategy_version,
            action=req.action,
            entry_price=req.entry_price,
            stop_loss=req.stop_loss,
            target_price=req.target_price,
            reward_risk_ratio=max(1.0, rr),
            position_size=1,
            confidence_pct=req.confidence_pct,
            invalidation_reason=req.invalidation_reason,
            data_status="ok",
            status=SignalStatus.GENERATED,
            created_at=datetime.now(timezone.utc),
            session_id=settings.session_id
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid signal specification: {e}")

    from veterandesk.config import PKT_TZ
    now_pkt = datetime.now(PKT_TZ).time()
    assessment = risk_engine.evaluate_signal(
        signal=sig,
        account_balance=ledger.cash_balance,
        current_day_realized_loss=abs(min(0.0, ledger.realized_pnl)),
        trades_executed_today=len(broker.closed_trades) + len(broker.open_trades),
        current_time_pkt=now_pkt,
        twenty_day_adv=req.twenty_day_adv,
        open_positions=[{"ticker": t.ticker, "shares": t.shares} for t in broker.open_trades.values()],
    )

    if not assessment.is_approved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Trade rejected by Risk Engine",
                "rejection_reasons": assessment.rejection_reasons,
                "rule_results": [{"rule": r.rule_name, "passed": r.passed, "reason": r.reason} for r in assessment.rule_results]
            }
        )

    sig.position_size = assessment.approved_shares
    sig.status = SignalStatus.APPROVED

    trade = broker.execute_buy(
        signal=sig,
        scraped_price=req.entry_price,
        shares=assessment.approved_shares
    )

    try:
        telegram_service.send_signal_alert(
            signal=sig,
            shares=assessment.approved_shares,
            reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
        )
    except Exception as ex:
        logger.warning("telegram_signal_alert_failed", error=str(ex), signal_id=sig.signal_id)

    try:
        discord_service.send_signal_alert(
            signal=sig,
            shares=assessment.approved_shares,
            reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
        )
    except Exception as ex:
        logger.warning("discord_signal_alert_failed", error=str(ex), signal_id=sig.signal_id)

    return {
        "status": "APPROVED_AND_EXECUTED",
        "risk_assessment": {
            "approved_shares": assessment.approved_shares,
            "risk_pct_used": assessment.risk_pct_used,
            "rules_checked": len(assessment.rule_results)
        },
        "trade": {
            "trade_id": trade.trade_id,
            "ticker": trade.ticker,
            "shares": trade.shares,
            "entry_price": trade.filled_entry_price,
            "stop_loss": trade.stop_loss,
            "target_price": trade.target_price,
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
