"""
Double-Entry Ledger Module for VeteranDesk.

Core Invariants:
1. Every transaction must be balanced: SUM(Debits) == SUM(Credits).
2. Atomic consistency: All entries for a transaction commit or none commit.
3. System Invariant Reconciliation:
   Cash Balance + Positions Value == Starting Balance + Realized Net P&L.
   Reconciliation runs after every fill and end-of-day.
   Any mismatch immediately raises an alert and freezes trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
import uuid

from veterandesk.logging import get_logger

logger = get_logger("veterandesk.ledger")


class AccountType(str, Enum):
    CASH = "CASH"
    EQUITY_HOLDINGS = "EQUITY_HOLDINGS"
    COMMISSION_EXPENSE = "COMMISSION_EXPENSE"
    TAX_EXPENSE = "TAX_EXPENSE"
    REALIZED_PNL = "REALIZED_PNL"


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    transaction_id: str
    trade_id: Optional[str]
    account: AccountType
    debit: float
    credit: float
    balance_after: float
    description: str
    created_at: datetime


class DoubleEntryLedger:
    """
    In-memory / persistent double-entry bookkeeping ledger.
    """

    def __init__(self, starting_balance_pkr: float = 500000.0, load_from_db: bool = True) -> None:
        self.starting_balance: float = starting_balance_pkr
        self.entries: List[LedgerEntry] = []
        self._account_balances: Dict[AccountType, float] = {
            AccountType.CASH: starting_balance_pkr,
            AccountType.EQUITY_HOLDINGS: 0.0,
            AccountType.COMMISSION_EXPENSE: 0.0,
            AccountType.TAX_EXPENSE: 0.0,
            AccountType.REALIZED_PNL: 0.0,
        }
        
        if load_from_db:
            self._load_state_from_db()

    def _load_state_from_db(self) -> None:
        """
        Load ledger entries from Supabase demo_ledger table and reconstruct account balances.
        This ensures ledger state persists across worker restarts.
        """
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            
            # Fetch all ledger entries ordered by creation time
            # Use simpler query to avoid "JSON could not be generated" error
            res = client.table("demo_ledger").select("*").execute()
            
            if not hasattr(res, "data") or not res.data:
                logger.info("ledger_db_empty_starting_fresh", starting_balance=self.starting_balance)
                return
            
            if len(res.data) == 0:
                logger.info("ledger_db_empty_starting_fresh", starting_balance=self.starting_balance)
                return
            
            # Reconstruct ledger state from database entries
            loaded_entries: List[LedgerEntry] = []
            reconstructed_balances: Dict[AccountType, float] = {
                AccountType.CASH: self.starting_balance,
                AccountType.EQUITY_HOLDINGS: 0.0,
                AccountType.COMMISSION_EXPENSE: 0.0,
                AccountType.TAX_EXPENSE: 0.0,
                AccountType.REALIZED_PNL: 0.0,
            }
            
            # Sort entries by creation time to ensure correct order
            sorted_entries = sorted(res.data, key=lambda x: x.get("created_at", ""))
            
            for row in sorted_entries:
                try:
                    account_str = row.get("account_name")
                    if not account_str:
                        continue
                    
                    try:
                        account = AccountType(account_str)
                    except ValueError:
                        logger.warning("ledger_db_unknown_account_type", account_type=account_str)
                        continue
                    
                    debit = float(row.get("debit", 0.0))
                    credit = float(row.get("credit", 0.0))
                    balance_after = float(row.get("balance_after", 0.0))
                    
                    # Reconstruct entry
                    entry = LedgerEntry(
                        id=str(row.get("id", "")),
                        transaction_id=row.get("transaction_id", ""),
                        trade_id=row.get("trade_id"),
                        account=account,
                        debit=debit,
                        credit=credit,
                        balance_after=balance_after,
                        description=row.get("description", ""),
                        created_at=datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
                    )
                    loaded_entries.append(entry)
                    
                    # Reconstruct account balance
                    if account in (AccountType.CASH, AccountType.EQUITY_HOLDINGS, AccountType.COMMISSION_EXPENSE, AccountType.TAX_EXPENSE):
                        reconstructed_balances[account] += (debit - credit)
                    elif account == AccountType.REALIZED_PNL:
                        reconstructed_balances[account] += (credit - debit)
                        
                except Exception as e:
                    logger.warning("ledger_db_entry_reconstruction_failed", entry_id=row.get("id"), error=str(e))
                    continue
            
            # Update ledger state with reconstructed data
            self.entries = loaded_entries
            self._account_balances = reconstructed_balances
            
            logger.info(
                "ledger_state_loaded_from_db",
                entries_count=len(loaded_entries),
                cash_balance=reconstructed_balances[AccountType.CASH],
                equity_holdings=reconstructed_balances[AccountType.EQUITY_HOLDINGS],
                realized_pnl=reconstructed_balances[AccountType.REALIZED_PNL]
            )
            
        except Exception as e:
            logger.critical("ledger_db_load_failed_critical", error=str(e), error_type=type(e).__name__)
            # Fail loudly instead of silently defaulting to starting balance
            raise RuntimeError(f"CRITICAL: Failed to load ledger state from database: {e}") from e

    @property
    def cash_balance(self) -> float:
        return self._account_balances[AccountType.CASH]

    @property
    def equity_holdings_value(self) -> float:
        return self._account_balances[AccountType.EQUITY_HOLDINGS]

    @property
    def total_commissions(self) -> float:
        return self._account_balances[AccountType.COMMISSION_EXPENSE]

    @property
    def total_taxes(self) -> float:
        return self._account_balances[AccountType.TAX_EXPENSE]

    @property
    def realized_pnl(self) -> float:
        return self._account_balances[AccountType.REALIZED_PNL]

    @property
    def total_equity(self) -> float:
        """
        Total portfolio equity: Cash + Equity Holdings (at cost basis).
        Reconciled against: Starting Balance + Realized P&L - (Commissions + Taxes).
        """
        return self.cash_balance + self.equity_holdings_value

    def record_transaction(
        self,
        transaction_id: str,
        trade_id: Optional[str],
        description: str,
        items: List[tuple[AccountType, float, float]],  # (Account, Debit, Credit)
        timestamp: Optional[datetime] = None
    ) -> List[LedgerEntry]:
        """
        Record a balanced transaction.
        Enforces SUM(Debits) == SUM(Credits) to within 1e-4 tolerance.
        """
        ts = timestamp or datetime.now(timezone.utc)
        total_debits = sum(d for _, d, _ in items)
        total_credits = sum(c for _, _, c in items)

        if abs(total_debits - total_credits) > 0.0001:
            err_msg = (
                f"Ledger Imbalance in transaction {transaction_id}! "
                f"Debits: {total_debits:.4f} != Credits: {total_credits:.4f}"
            )
            logger.critical("ledger_imbalance_detected", error=err_msg)
            raise ValueError(err_msg)

        new_entries: List[LedgerEntry] = []

        for acct, debit, credit in items:
            # Update balance based on standard accounting rules:
            # Asset & Expense increase on Debit, decrease on Credit.
            # Equity/Revenue (Realized P&L) increases on Credit, decreases on Debit.
            if acct in (AccountType.CASH, AccountType.EQUITY_HOLDINGS, AccountType.COMMISSION_EXPENSE, AccountType.TAX_EXPENSE):
                self._account_balances[acct] += (debit - credit)
            elif acct == AccountType.REALIZED_PNL:
                self._account_balances[acct] += (credit - debit)

            entry = LedgerEntry(
                id=str(uuid.uuid4()),
                transaction_id=transaction_id,
                trade_id=trade_id,
                account=acct,
                debit=round(debit, 4),
                credit=round(credit, 4),
                balance_after=round(self._account_balances[acct], 4),
                description=description,
                created_at=ts
            )
            new_entries.append(entry)
            self.entries.append(entry)

        # Run reconciliation verification
        is_reconciled, diff, message = self.reconcile()
        if not is_reconciled:
            logger.critical("ledger_reconciliation_failed", diff=diff, message=message)
            raise RuntimeError(f"CRITICAL: Ledger reconciliation failure after tx {transaction_id}: {message}")

        return new_entries

    def reconcile(self) -> tuple[bool, float, str]:
        """
        Verify the fundamental system reconciliation equation:
        Assets = Initial Capital + Realized P&L - Expenses
        Cash + Holdings = Starting Balance + Realized P&L - (Commissions + Taxes)
        """
        current_assets = self.cash_balance + self.equity_holdings_value
        expected_assets = (
            self.starting_balance 
            + self.realized_pnl 
            - (self.total_commissions + self.total_taxes)
        )
        diff = round(current_assets - expected_assets, 4)

        if abs(diff) > 0.01:
            msg = (
                f"Assets (PKR {current_assets:,.2f}) != Expected (PKR {expected_assets:,.2f}). "
                f"Discrepancy: PKR {diff:,.2f}"
            )
            return False, diff, msg

        return True, 0.0, "Ledger perfectly reconciled."

    def recompute_from_scratch(self) -> tuple[bool, float, str]:
        """
        Audit function: Recompute all account balances from raw entries
        and verify they match running totals exactly.
        """
        audit_balances: Dict[AccountType, float] = {
            AccountType.CASH: self.starting_balance,
            AccountType.EQUITY_HOLDINGS: 0.0,
            AccountType.COMMISSION_EXPENSE: 0.0,
            AccountType.TAX_EXPENSE: 0.0,
            AccountType.REALIZED_PNL: 0.0,
        }

        for entry in self.entries:
            acct = entry.account
            if acct in (AccountType.CASH, AccountType.EQUITY_HOLDINGS, AccountType.COMMISSION_EXPENSE, AccountType.TAX_EXPENSE):
                audit_balances[acct] += (entry.debit - entry.credit)
            elif acct == AccountType.REALIZED_PNL:
                audit_balances[acct] += (entry.credit - entry.debit)

        for acct, bal in audit_balances.items():
            diff = abs(bal - self._account_balances[acct])
            if diff > 0.01:
                return False, diff, f"Audit mismatch in {acct.value}: recomputed={bal}, running={self._account_balances[acct]}"

        return True, 0.0, "All ledger accounts match recomputed totals."
