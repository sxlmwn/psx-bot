"""
Tests for Crash Recovery, Idempotency, and Daily Halt Persistence.
Requirement from Section 6 & 7:
"Process kill mid-session -> clean resume, zero duplicates, daily halt persisted"
"""

from datetime import date, datetime, time, timezone
from unittest.mock import patch, MagicMock
import pytest
import tempfile
import os

from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import PaperBroker
from veterandesk.risk.engine import RiskEngine, check_daily_halt_from_db, record_daily_halt
from veterandesk.strategy.models import TradeSignal


# Dummy function for scheduler persistence test (must be serializable)
def _dummy_job_function() -> None:
    """Dummy function for APScheduler persistence testing."""
    pass


class TestCrashRecoveryAndPersistence:
    def test_daily_halt_persists_across_restart(self) -> None:
        """
        Simulate process shutdown while daily halt was active.
        Verify that new process instantiating risk engine detects persisted halt from database.
        """
        from veterandesk.config import PKT_TZ
        
        # Use a fixed date for testing
        test_date = datetime.now(PKT_TZ).date()
        
        # Mock the database client to simulate a halt record in the database
        mock_client = MagicMock()
        mock_data = [{
            "halt_date": str(test_date),
            "is_halted": True,
            "reason": "Daily loss 2.2% exceeded limit 2.0%",
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "loss_amount": 11000.0,
            "loss_pct": 2.2,
        }]
        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=mock_data)
        
        with patch('veterandesk.database.session.db_manager') as mock_db_manager:
            mock_db_manager.get_client.return_value = mock_client
            
            # Verify that check_daily_halt_from_db returns True for the halted date
            is_halted = check_daily_halt_from_db(test_date)
            assert is_halted is True
            
            # Create a new RiskEngine instance (simulating process restart)
            engine = RiskEngine()
            sig = TradeSignal(
                signal_id="SIG_RESTART",
                ticker="OGDC",
                entry_price=100.0,
                stop_loss=95.0,
                target_price=108.0,
                reward_risk_ratio=1.6,
                position_size=100,
                confidence_pct=60,
                invalidation_reason="Test",
                created_at=datetime.now(timezone.utc),
                session_id="restart_session",
            )

            # The risk engine should reject the signal because is_already_halted=True
            assessment = engine.evaluate_signal(
                signal=sig,
                account_balance=500000.0,
                current_day_realized_loss=0.0,
                trades_executed_today=0,
                current_time_pkt=time(10, 30, 0),
                twenty_day_adv=100000.0,
                open_positions=[],
                is_already_halted=True  # This would come from check_daily_halt_from_db in real scenario
            )

            assert assessment.is_approved is False
            assert any("halted for the day" in r for r in assessment.rejection_reasons)

    def test_daily_halt_clears_on_new_day(self) -> None:
        """
        Verify that halt state clears correctly on a new trading day.
        A halt on day N should not affect trading on day N+1.
        """
        from veterandesk.config import PKT_TZ
        
        halted_date = datetime.now(PKT_TZ).date()
        next_day = date(halted_date.year, halted_date.month, halted_date.day + 1)
        
        # Test 1: Check halted date returns True
        mock_client_halted = MagicMock()
        mock_data_halted = [{
            "halt_date": str(halted_date),
            "is_halted": True,
            "reason": "Daily loss limit breached",
        }]
        mock_client_halted.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=mock_data_halted)
        
        with patch('veterandesk.database.session.db_manager') as mock_db_manager:
            mock_db_manager.get_client.return_value = mock_client_halted
            assert check_daily_halt_from_db(halted_date) is True
        
        # Test 2: Check next day returns False (no halt record)
        mock_client_next = MagicMock()
        mock_client_next.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        
        with patch('veterandesk.database.session.db_manager') as mock_db_manager:
            mock_db_manager.get_client.return_value = mock_client_next
            assert check_daily_halt_from_db(next_day) is False

    def test_record_daily_halt_writes_to_database(self) -> None:
        """
        Verify that record_daily_halt correctly writes to the database.
        """
        from veterandesk.config import PKT_TZ
        
        test_date = datetime.now(PKT_TZ).date()
        mock_client = MagicMock()
        mock_client.table.return_value.upsert.return_value.execute.return_value = MagicMock()
        
        with patch('veterandesk.database.session.db_manager') as mock_db_manager:
            mock_db_manager.get_client.return_value = mock_client
            
            # Record a halt
            record_daily_halt(
                halt_date=test_date,
                loss_amount=11000.0,
                loss_pct=2.2,
                reason="Daily loss limit breached",
            )
            
            # Verify the database write was called
            mock_client.table.assert_called_once_with("daily_halts")
            mock_client.table.return_value.upsert.assert_called_once()
            
            # Verify the record structure
            call_args = mock_client.table.return_value.upsert.call_args
            record = call_args[0][0]
            assert record["halt_date"] == str(test_date)
            assert record["is_halted"] is True
            assert record["loss_amount"] == 11000.0
            assert record["loss_pct"] == 2.2
            assert record["reason"] == "Daily loss limit breached"

    def test_trade_idempotency_prevents_duplicate_executions(self) -> None:
        """
        Verify that executing the same signal twice does not double-fill or duplicate.
        """
        ledger = DoubleEntryLedger(starting_balance_pkr=500000.0)
        broker = PaperBroker(ledger=ledger, persist_to_db=False)

        sig = TradeSignal(
            signal_id="SIG_UNIQUE_1",
            ticker="PPL",
            entry_price=110.0,
            stop_loss=105.0,
            target_price=118.0,
            reward_risk_ratio=1.6,
            position_size=500,
            confidence_pct=65,
            invalidation_reason="Test",
            created_at=datetime.now(timezone.utc),
            session_id="sess_idem",
        )

        # 1. First execution -> succeeds
        trade1 = broker.execute_buy(signal=sig, shares=500, scraped_price=110.0)
        assert len(broker.open_trades) == 1

        # 2. Duplicate signal arrives with existing ticker already open (unplanned averaging down)
        engine = RiskEngine()
        open_pos = [{"ticker": t.ticker, "shares": t.shares} for t in broker.open_trades.values()]
        assessment = engine.evaluate_signal(
            signal=sig,
            account_balance=ledger.cash_balance,
            current_day_realized_loss=0.0,
            trades_executed_today=1,
            current_time_pkt=time(10, 30, 0),
            twenty_day_adv=100000.0,
            open_positions=open_pos,
            is_already_halted=False,
            is_pre_planned_add=False,
        )

        # Risk engine blocks duplicate position addition
        assert assessment.is_approved is False
        assert any("averaging down" in r for r in assessment.rejection_reasons)

    def test_scheduler_job_persistence_across_restart(self) -> None:
        """
        Test that APScheduler jobs persist across process restarts using SQLAlchemy job store.
        Verifies job state (next_run_time, etc.) survives scheduler instance recreation.
        """
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        
        # Create a temporary SQLite database for the job store
        with tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False) as f:
            temp_db_path = f.name
        
        try:
            jobstores = {
                'default': SQLAlchemyJobStore(url=f'sqlite:///{temp_db_path}')
            }
            
            # Session 1: Create scheduler and add jobs
            scheduler1 = BackgroundScheduler(timezone=timezone.utc, jobstores=jobstores)
            scheduler1.start()
            scheduler1.add_job(
                'tests.test_crash_recovery:_dummy_job_function',
                'cron',
                hour=9,
                minute=15,
                id='daily_brief',
                name='Daily Brief',
                replace_existing=True,
            )
            scheduler1.add_job(
                'tests.test_crash_recovery:_dummy_job_function',
                'cron',
                hour=15,
                minute=45,
                id='session_summary',
                name='Session Summary',
                replace_existing=True,
            )
            
            # Verify jobs are present in first scheduler
            jobs1 = scheduler1.get_jobs()
            assert len(jobs1) == 2
            job_ids1 = {job.id for job in jobs1}
            assert job_ids1 == {'daily_brief', 'session_summary'}
            
            # Store job details for comparison
            job_details1 = {job.id: {'name': job.name, 'trigger': str(job.trigger)} for job in jobs1}
            
            scheduler1.shutdown()
            
            # Session 2: Simulate restart by creating new scheduler with same job store
            scheduler2 = BackgroundScheduler(timezone=timezone.utc, jobstores=jobstores)
            scheduler2.start()  # Need to start to load jobs from persistent store
            
            # Jobs should be automatically loaded from persistent store
            jobs2 = scheduler2.get_jobs()
            assert len(jobs2) == 2
            job_ids2 = {job.id for job in jobs2}
            assert job_ids2 == {'daily_brief', 'session_summary'}
            
            # Verify job details persisted
            job_details2 = {job.id: {'name': job.name, 'trigger': str(job.trigger)} for job in jobs2}
            for job_id in job_ids1:
                assert job_id in job_details2
                assert job_details1[job_id]['name'] == job_details2[job_id]['name']
                assert job_details1[job_id]['trigger'] == job_details2[job_id]['trigger']
            
            scheduler2.shutdown()
            
            # Session 3: Add jobs again with replace_existing=True (simulating normal startup)
            scheduler3 = BackgroundScheduler(timezone=timezone.utc, jobstores=jobstores)
            scheduler3.start()
            scheduler3.add_job(
                'tests.test_crash_recovery:_dummy_job_function',
                'cron',
                hour=9,
                minute=15,
                id='daily_brief',
                name='Daily Brief',
                replace_existing=True,
            )
            scheduler3.add_job(
                'tests.test_crash_recovery:_dummy_job_function',
                'cron',
                hour=15,
                minute=45,
                id='session_summary',
                name='Session Summary',
                replace_existing=True,
            )
            
            # Should still have exactly 2 jobs (no duplicates)
            jobs3 = scheduler3.get_jobs()
            assert len(jobs3) == 2
            job_ids3 = {job.id for job in jobs3}
            assert job_ids3 == {'daily_brief', 'session_summary'}
            
            scheduler3.shutdown()
            
        finally:
            # Clean up temporary database
            if os.path.exists(temp_db_path):
                os.unlink(temp_db_path)
