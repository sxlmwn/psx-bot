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

    def __init__(self, starting_balance_pkr: float = 500000.0) -> None:
        self.starting_balance: float = starting_balance_pkr
        self.entries: List[LedgerEntry] = []
        self._account_balances: Dict[AccountType, float] = {
            AccountType.CASH: starting_balance_pkr,
            AccountType.EQUITY_HOLDINGS: 0.0,
            AccountType.COMMISSION_EXPENSE: 0.0,
            AccountType.TAX_EXPENSE: 0.0,
            AccountType.REALIZED_PNL: 0.0,
        }

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
