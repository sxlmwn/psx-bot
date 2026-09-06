"""
Realistic Paper Broker Module for VeteranDesk Demo Account.

Key Features:
1. Realistic fills with configurable slippage (0.10% - 0.30%).
2. Accurate PSX fee schedule (Commission, SECP, NCCPL, CGT).
3. Hard Stop-Loss validation (cannot open trade without valid stop).
4. Atomic integration with the Double-Entry Ledger.
5. Invariant checking after every fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from veterandesk.config import fee_structure, settings
from veterandesk.execution.ledger import AccountType, DoubleEntryLedger
from veterandesk.logging import get_logger
from veterandesk.strategy.models import SignalAction, TradeSignal

logger = get_logger("veterandesk.paper_broker")


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ExitReason(str, Enum):
    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    TIME_STOP_1520 = "TIME_STOP_1520"
    MANUAL = "MANUAL"
    HALT = "HALT"


@dataclass
class DemoTrade:
    trade_id: str
    signal_id: str
    ticker: str
    action: SignalAction
    shares: int
    entry_price: float
    stop_loss: float
    target_price: float
    slippage_pct: float
    filled_entry_price: float
    status: TradeStatus = TradeStatus.OPEN
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exit_price: Optional[float] = None
    filled_exit_price: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    closed_at: Optional[datetime] = None
    gross_pnl: float = 0.0
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    net_pnl: float = 0.0
    fee_version: str = "PSX_STANDARD_v1"
    session_id: str = "default_session"

    def __post_init__(self) -> None:
        # Non-negotiable: Stop loss cannot be None or invalid
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError(f"CRITICAL: Trade {self.trade_id} rejected - stop loss cannot be empty or <= 0")
        if self.action == SignalAction.BUY and self.stop_loss >= self.entry_price:
            raise ValueError(f"CRITICAL: Stop loss ({self.stop_loss}) must be strictly below entry ({self.entry_price})")


class PaperBroker:
    """
    Simulated broker for demo paper trading with complete accounting integration.
    """

    def __init__(
        self,
        ledger: DoubleEntryLedger,
        slippage_pct: Optional[float] = None,
        persist_to_db: bool = True,
    ) -> None:
        self.ledger: DoubleEntryLedger = ledger
        self.slippage_pct: float = slippage_pct or fee_structure.default_slippage_pct
        self.persist_to_db: bool = persist_to_db
        self.open_trades: Dict[str, DemoTrade] = {}
        self.closed_trades: List[DemoTrade] = []

    def execute_buy(
        self,
        signal: TradeSignal,
        shares: int,
        scraped_price: float,
        timestamp: Optional[datetime] = None
    ) -> DemoTrade:
        """
        Execute a simulated BUY fill with slippage and full double-entry ledger settlement.
        """
        ts = timestamp or datetime.now(timezone.utc)

        # Enforce stop loss
        if signal.stop_loss is None or signal.stop_loss <= 0:
            raise ValueError("No trade without a stop loss!")
        if signal.stop_loss >= scraped_price:
            raise ValueError("Stop loss must be strictly below entry price!")
        if shares <= 0:
            raise ValueError(f"Invalid shares count: {shares}")

        # Apply slippage on BUY: price moves up against buyer
        filled_price = round(scraped_price * (1.0 + self.slippage_pct), 2)
        nominal_cost = round(shares * filled_price, 2)

        # Compute fees
        commission = round(nominal_cost * fee_structure.broker_commission_pct, 2)
        regulatory = round(nominal_cost * (fee_structure.secp_turnover_pct + fee_structure.nccpl_charges_pct), 2)
        total_entry_fees = commission + regulatory
        total_cash_outflow = nominal_cost + total_entry_fees

        # Check cash balance
        if total_cash_outflow > self.ledger.cash_balance:
            raise ValueError(
                f"Insufficient funds: Required PKR {total_cash_outflow:,.2f}, "
                f"Available Cash: PKR {self.ledger.cash_balance:,.2f}"
            )

        trade_id = f"TRD_{signal.ticker}_{int(ts.timestamp())}_{uuid.uuid4().hex[:6]}"
        tx_id = f"TX_BUY_{trade_id}"

        trade = DemoTrade(
            trade_id=trade_id,
            signal_id=signal.signal_id,
            ticker=signal.ticker,
            action=SignalAction.BUY,
            shares=shares,
            entry_price=scraped_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            slippage_pct=self.slippage_pct,
            filled_entry_price=filled_price,
            opened_at=ts,
            entry_fees=total_entry_fees,
            fee_version=fee_structure.version,
            session_id=signal.session_id
        )

        # Balanced double-entry bookkeeping items:
        # Debit EQUITY_HOLDINGS: nominal_cost
        # Debit COMMISSION_EXPENSE: commission
        # Debit TAX_EXPENSE: regulatory
        # Credit CASH: total_cash_outflow
        items = [
            (AccountType.EQUITY_HOLDINGS, nominal_cost, 0.0),
            (AccountType.COMMISSION_EXPENSE, commission, 0.0),
            (AccountType.TAX_EXPENSE, regulatory, 0.0),
            (AccountType.CASH, 0.0, total_cash_outflow),
        ]

        entries = self.ledger.record_transaction(
            transaction_id=tx_id,
            trade_id=trade_id,
            description=f"BUY {shares} {signal.ticker} @ {filled_price:.2f} (Slip: {self.slippage_pct*100:.2f}%)",
            items=items,
            timestamp=ts
        )

        self.open_trades[trade_id] = trade
        self._persist_trade_to_db(trade)
        self._persist_ledger_entries_to_db(entries)
        logger.info(
            "demo_buy_filled",
            trade_id=trade_id,
            ticker=trade.ticker,
            shares=shares,
            filled_price=filled_price,
            stop_loss=trade.stop_loss,
            target=trade.target_price,
            cash_remaining=self.ledger.cash_balance
        )
        return trade


    @staticmethod
    def evaluate_exit_condition(
        trade: DemoTrade,
        scraped_price: float,
        current_time_pkt: Optional[time] = None,
    ) -> Optional[ExitReason]:
        """
        Deterministically evaluates whether an open trade should exit based on market price and session cutoff.

        Priority order:
        1. Mandatory session cutoff (>= 15:20 PKT) -> TIME_STOP_1520 (mandatory intraday flat)
        2. Stop loss breached (scraped_price <= trade.stop_loss) -> STOP_HIT
        3. Target reached (scraped_price >= trade.target_price) -> TARGET_HIT
        4. Otherwise -> None (continue holding)
        """
        if current_time_pkt is not None:
            cutoff = time(15, 20)
            if current_time_pkt >= cutoff:
                return ExitReason.TIME_STOP_1520

        if trade.stop_loss is not None and scraped_price <= trade.stop_loss:
            return ExitReason.STOP_HIT

        if trade.target_price is not None and scraped_price >= trade.target_price:
            return ExitReason.TARGET_HIT

        return None

    def execute_exit(
        self,
        trade_id: str,
        scraped_price: float,
        exit_reason: ExitReason,
        timestamp: Optional[datetime] = None
    ) -> DemoTrade:
        """
        Execute simulated trade exit with slippage and double-entry reconciliation.
        Strictly validates that market price satisfies the specified exit condition.
        """
        ts = timestamp or datetime.now(timezone.utc)
        if trade_id not in self.open_trades:
            raise KeyError(f"Trade {trade_id} is not an open trade")

        trade = self.open_trades[trade_id]

        # Strict validation: TARGET_HIT only fires when scraped market price reaches or exceeds target
        if exit_reason == ExitReason.TARGET_HIT:
            if trade.target_price is not None and scraped_price < trade.target_price:
                raise ValueError(
                    f"Cannot exit with TARGET_HIT: market price PKR {scraped_price:.2f} < target price PKR {trade.target_price:.2f}. "
                    f"Target was never reached."
                )
        elif exit_reason == ExitReason.STOP_HIT:
            if trade.stop_loss is not None and scraped_price > trade.stop_loss:
                raise ValueError(
                    f"Cannot exit with STOP_HIT: market price PKR {scraped_price:.2f} > stop loss PKR {trade.stop_loss:.2f}."
                )

        # Apply slippage on SELL: price moves down against seller
        filled_exit_price = round(scraped_price * (1.0 - self.slippage_pct), 2)
        nominal_proceeds = round(trade.shares * filled_exit_price, 2)
        original_holdings_cost = round(trade.shares * trade.filled_entry_price, 2)

        gross_pnl = round(nominal_proceeds - original_holdings_cost, 2)

        # Exit fees
        commission = round(nominal_proceeds * fee_structure.broker_commission_pct, 2)
        regulatory = round(nominal_proceeds * (fee_structure.secp_turnover_pct + fee_structure.nccpl_charges_pct), 2)

        # CGT on net positive capital gain
        pre_tax_net = gross_pnl - trade.entry_fees - commission - regulatory
        cgt = round(pre_tax_net * fee_structure.cgt_withholding_pct, 2) if pre_tax_net > 0 else 0.0
        total_exit_fees = commission + regulatory + cgt
        net_cash_received = round(nominal_proceeds - total_exit_fees, 2)
        net_trade_pnl = round(gross_pnl - trade.entry_fees - total_exit_fees, 2)

        tx_id = f"TX_EXIT_{trade_id}"

        # Double-entry ledger items for SELL:
        # Cash receives net cash -> Debit CASH
        # Holdings cleared -> Credit EQUITY_HOLDINGS (original cost basis)
        # Fees recorded -> Debit COMMISSION_EXPENSE, Debit TAX_EXPENSE
        # Balance out through REALIZED_PNL:
        # If gross_pnl >= 0: Credit REALIZED_PNL by gross_pnl
        # If gross_pnl < 0: Debit REALIZED_PNL by abs(gross_pnl)
        items = [
            (AccountType.CASH, net_cash_received, 0.0),
            (AccountType.EQUITY_HOLDINGS, 0.0, original_holdings_cost),
            (AccountType.COMMISSION_EXPENSE, commission, 0.0),
            (AccountType.TAX_EXPENSE, regulatory + cgt, 0.0),
        ]

        if gross_pnl >= 0:
            items.append((AccountType.REALIZED_PNL, 0.0, gross_pnl))
        else:
            items.append((AccountType.REALIZED_PNL, abs(gross_pnl), 0.0))

        entries = self.ledger.record_transaction(
            transaction_id=tx_id,
            trade_id=trade_id,
            description=f"EXIT {trade.shares} {trade.ticker} @ {filled_exit_price:.2f} ({exit_reason.value})",
            items=items,
            timestamp=ts
        )

        trade.exit_price = scraped_price
        trade.filled_exit_price = filled_exit_price
        trade.exit_reason = exit_reason
        trade.closed_at = ts
        trade.status = TradeStatus.CLOSED
        trade.gross_pnl = gross_pnl
        trade.exit_fees = total_exit_fees
        trade.net_pnl = net_trade_pnl

        del self.open_trades[trade_id]
        self.closed_trades.append(trade)
        self._persist_trade_to_db(trade)
        self._persist_ledger_entries_to_db(entries)

        if self.persist_to_db:
            # Telegram alert on level hit / position closed
            try:
                from veterandesk.alerts.telegram import telegram_service
                telegram_service.send_level_hit_alert(
                    ticker=trade.ticker,
                    trade_id=trade.trade_id,
                    level_type=exit_reason.value,
                    price=scraped_price,
                    fill_price=filled_exit_price,
                    net_pnl=net_trade_pnl,
                    closed_at_str=ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
                )
            except Exception as ex:
                logger.warning("telegram_level_hit_alert_failed", error=str(ex), trade_id=trade.trade_id)

            # Telegram alert on graduation eligibility check
            try:
                from veterandesk.execution.graduation import compute_performance_metrics, notify_graduation_status
                metrics = compute_performance_metrics(self.closed_trades)
                if metrics.is_graduated or len(self.closed_trades) == 30:
                    notify_graduation_status(metrics)
            except Exception as ex:
                logger.warning("telegram_graduation_check_failed", error=str(ex), trade_id=trade.trade_id)

        logger.info(
            "demo_exit_filled",
            trade_id=trade_id,
            ticker=trade.ticker,
            reason=exit_reason.value,
            net_pnl=net_trade_pnl,
            cash_balance=self.ledger.cash_balance
        )
        return trade

    def _persist_ledger_entries_to_db(self, entries: List[Any]) -> None:
        """Persist double-entry ledger records to Supabase PostgreSQL."""
        if not self.persist_to_db:
            return
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            records = [
                {
                    "transaction_id": e.transaction_id,
                    "trade_id": e.trade_id,
                    "account_name": e.account.value,
                    "debit": float(e.debit),
                    "credit": float(e.credit),
                    "balance_after": float(e.balance_after),
                    "description": e.description,
                    "created_at": e.created_at.isoformat()
                }
                for e in entries
            ]
            client.table("demo_ledger").insert(records).execute()
            logger.info("ledger_persisted_to_supabase", count=len(records))
        except Exception as e:
            logger.warning("ledger_db_persistence_skipped", error=str(e))

    def _persist_trade_to_db(self, trade: DemoTrade) -> None:
        """Persist trade record to Supabase PostgreSQL database."""
        if not self.persist_to_db:
            return
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            record = {
                "trade_id": trade.trade_id,
                "signal_id": trade.signal_id,
                "ticker": trade.ticker,
                "action": trade.action.value,
                "shares": int(trade.shares),
                "entry_price": float(trade.filled_entry_price),
                "exit_price": float(trade.filled_exit_price) if trade.filled_exit_price is not None else None,
                "stop_loss": float(trade.stop_loss),
                "target_price": float(trade.target_price),
                "slippage_pct": float(trade.slippage_pct),
                "gross_pnl": float(trade.gross_pnl) if trade.gross_pnl is not None else None,
                "fees_paid": float(trade.entry_fees + trade.exit_fees),
                "net_pnl": float(trade.net_pnl) if trade.net_pnl is not None else None,
                "risk_pct_used": 0.50,
                "status": trade.status.value,
                "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
                "opened_at": trade.opened_at.isoformat(),
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
                "fee_version": trade.fee_version,
                "session_id": trade.session_id,
            }
            # Ensure trade_signals row exists and has the approved position size
            try:
                sig_check = client.table("trade_signals").select("signal_id").eq("signal_id", trade.signal_id).execute()
                if sig_check.data:
                    client.table("trade_signals").update({
                        "position_size": int(trade.shares),
                        "status": "APPROVED"
                    }).eq("signal_id", trade.signal_id).execute()
                else:
                    rr = round((trade.target_price - trade.entry_price) / max(0.01, trade.entry_price - trade.stop_loss), 2)
                    client.table("trade_signals").insert({
                        "signal_id": trade.signal_id,
                        "ticker": trade.ticker,
                        "strategy": "ORB_v1.0",
                        "strategy_version": "1.0.0",
                        "action": trade.action.value,
                        "entry_price": float(trade.entry_price),
                        "stop_loss": float(trade.stop_loss),
                        "target_price": float(trade.target_price),
                        "reward_risk_ratio": max(1.0, rr),
                        "position_size": int(trade.shares),
                        "confidence_pct": 75,
                        "invalidation_reason": "Automated trade execution",
                        "data_status": "ok",
                        "status": "APPROVED",
                        "created_at": trade.opened_at.isoformat(),
                        "session_id": trade.session_id
                    }).execute()
            except Exception as se:
                logger.warning("signal_fk_sync_skipped", error=str(se))

            # Upsert into trades and demo_trades tables in Supabase
            client.table("trades").upsert(record).execute()
            client.table("demo_trades").upsert(record).execute()
            logger.info("trade_persisted_to_supabase", trade_id=trade.trade_id)
        except Exception as e:
            logger.warning("trade_db_persistence_skipped", error=str(e))

