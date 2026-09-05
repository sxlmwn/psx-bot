"""
Tests for Double-Entry Ledger and Paper Broker.
Requirement from Section 7:
"Ledger tests: Reconciliation holds across 1,000 random simulated fills"
"""

import random
from datetime import datetime, timezone
import pytest

from veterandesk.config import fee_structure
from veterandesk.execution.ledger import AccountType, DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker
from veterandesk.strategy.models import SignalAction, TradeSignal


class TestDoubleEntryLedger:
    def test_single_buy_and_sell_reconciliation(self):
        ledger = DoubleEntryLedger(starting_balance_pkr=500000.0)
        broker = PaperBroker(ledger=ledger, slippage_pct=0.0020, persist_to_db=False)

        sig = TradeSignal(
            signal_id="SIG_TEST",
            ticker="OGDC",
            entry_price=100.0,
            stop_loss=95.0,
            target_price=107.5,
            reward_risk_ratio=1.5,
            position_size=1000,
            confidence_pct=60,
            invalidation_reason="Test invalidation",
            created_at=datetime.now(timezone.utc),
            session_id="sess_1"
        )

        # 1. Execute BUY
        trade = broker.execute_buy(signal=sig, shares=1000, scraped_price=100.0)
        assert trade.shares == 1000
        assert trade.filled_entry_price == 100.20  # 100 * (1 + 0.002)

        # Check ledger reconciliation
        is_rec, diff, msg = ledger.reconcile()
        assert is_rec is True, msg
        assert diff == 0.0

        # 2. Execute Exit on Target Hit (108.0)
        trade_exit = broker.execute_exit(
            trade_id=trade.trade_id,
            scraped_price=108.0,
            exit_reason=ExitReason.TARGET_HIT
        )
        assert trade_exit.status.value == "CLOSED"
        assert trade_exit.net_pnl > 0  # Profitable

        # Check ledger reconciliation again
        is_rec, diff, msg = ledger.reconcile()
        assert is_rec is True, msg
        assert diff == 0.0

        # Audit from scratch
        audit_ok, audit_diff, audit_msg = ledger.recompute_from_scratch()
        assert audit_ok is True, audit_msg

    def test_ledger_rejects_unbalanced_transactions(self):
        ledger = DoubleEntryLedger(starting_balance_pkr=500000.0)
        # Unbalanced: Debit 100 != Credit 50
        with pytest.raises(ValueError, match="Ledger Imbalance"):
            ledger.record_transaction(
                transaction_id="TX_BAD",
                trade_id=None,
                description="Unbalanced entry",
                items=[
                    (AccountType.CASH, 100.0, 0.0),
                    (AccountType.EQUITY_HOLDINGS, 0.0, 50.0),
                ]
            )

    def test_insufficient_funds_rejected(self):
        ledger = DoubleEntryLedger(starting_balance_pkr=10000.0)  # Low balance
        broker = PaperBroker(ledger=ledger, persist_to_db=False)

        sig = TradeSignal(
            signal_id="SIG_BIG",
            ticker="LUCK",
            entry_price=500.0,
            stop_loss=480.0,
            target_price=540.0,
            reward_risk_ratio=2.0,
            position_size=1000,
            confidence_pct=60,
            invalidation_reason="Test invalidation",
            created_at=datetime.now(timezone.utc),
            session_id="sess_1"
        )
        # 1000 * 500 = 500,000 PKR > 10,000 balance
        with pytest.raises(ValueError, match="Insufficient funds"):
            broker.execute_buy(signal=sig, shares=1000, scraped_price=500.0)

    def test_one_thousand_simulated_fills_reconciliation(self):
        """
        Non-negotiable requirement:
        Reconciliation holds across 1,000 random simulated fills with zero drift.
        """
        random.seed(42)  # Deterministic seed for reproducible tests
        starting_cash = 10000000.0  # 10M PKR to support 1,000 sequential trades
        ledger = DoubleEntryLedger(starting_balance_pkr=starting_cash)
        broker = PaperBroker(ledger=ledger, slippage_pct=0.0020, persist_to_db=False)

        tickers = ["OGDC", "PPL", "ENGRO", "LUCK", "HUBC", "MCB", "SYS", "TRG"]

        for i in range(500):  # 500 buys + 500 sells = 1,000 fills
            ticker = random.choice(tickers)
            base_price = round(random.uniform(50.0, 400.0), 2)
            stop_loss = round(base_price * 0.96, 2)
            risk = base_price - stop_loss
            target_price = round(base_price + (1.6 * risk), 2)
            rr = round((target_price - base_price) / risk, 2)
            shares = random.randint(50, 500)

            sig = TradeSignal(
                signal_id=f"SIG_SIM_{i}",
                ticker=ticker,
                entry_price=base_price,
                stop_loss=stop_loss,
                target_price=target_price,
                reward_risk_ratio=rr,
                position_size=shares,
                confidence_pct=random.randint(45, 75),
                invalidation_reason="Simulation",
                created_at=datetime.now(timezone.utc),
                session_id="sim_session"
            )

            # 1. Fill Buy
            trade = broker.execute_buy(signal=sig, shares=shares, scraped_price=base_price)

            # Invariant check immediately after buy
            is_rec, diff, msg = ledger.reconcile()
            assert is_rec is True, f"Buy fill {i} reconciliation failed: {msg}"

            # 2. Fill Exit (randomly hit target, stop, or close at market)
            outcome = random.choice(["target", "stop", "time"])
            if outcome == "target":
                exit_price = target_price
                reason = ExitReason.TARGET_HIT
            elif outcome == "stop":
                exit_price = stop_loss
                reason = ExitReason.STOP_HIT
            else:
                exit_price = round(base_price * random.uniform(0.97, 1.03), 2)
                reason = ExitReason.TIME_STOP_1520

            broker.execute_exit(
                trade_id=trade.trade_id,
                scraped_price=exit_price,
                exit_reason=reason
            )

            # Invariant check immediately after sell
            is_rec, diff, msg = ledger.reconcile()
            assert is_rec is True, f"Exit fill {i} reconciliation failed: {msg}"

        # Total entries in ledger: 500 buys * 4 entries + 500 sells * 5 entries = 4,500 entries
        assert len(ledger.entries) >= 4000
        assert len(broker.closed_trades) == 500

        # Full audit recomputed from raw entries
        audit_ok, audit_diff, audit_msg = ledger.recompute_from_scratch()
        assert audit_ok is True, audit_msg
        assert audit_diff == 0.0
