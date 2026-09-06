"""
Additional coverage tests targeting telegram alerts, API execution endpoints,
migration runner, database session utilities, and post-mortem edge cases.
Enforces overall test coverage >= 85% across veterandesk.
"""

from datetime import datetime, time, timedelta, timezone
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from fastapi.testclient import TestClient

from veterandesk.alerts.telegram import MessageType, OutboundMessage, TelegramService
from veterandesk.api.app import app
from veterandesk.config import settings
from veterandesk.database.migration import run_migration
from veterandesk.database.session import DatabaseManager
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker
from veterandesk.journal.post_mortem import (
    JournalRecord,
    PostMortemEngine,
    PostMortemStatus,
    TradeVerdict,
)
from veterandesk.strategy.models import SignalAction, TradeSignal


class TestCoverageExpansion:
    def test_telegram_templates_and_delivery(self):
        svc = TelegramService(bot_token="test_token", chat_id="12345", enabled=True)

        # 1. format_daily_brief
        brief = svc.format_daily_brief(
            date_str="2026-09-06",
            market_overview="KSE-100 consolidating around 78,500 level.",
            watchlist_summary=[
                {"ticker": "OGDC", "price": 328.80, "change_pct": 0.55},
                {"ticker": "HBL", "price": 313.68, "change_pct": -0.20},
            ],
            key_levels=["KSE-100: 78,200 support", "KSE-100: 79,000 resistance"],
        )
        assert "VETERANDESK DAILY BRIEF" in brief
        assert "OGDC" in brief
        assert "+0.55%" in brief

        # 2. format_session_summary
        summary = svc.format_session_summary(
            session_date="2026-09-06",
            trades_count=3,
            winning_trades=2,
            losing_trades=1,
            gross_pnl=12500.0,
            total_fees=3100.0,
            net_pnl=9400.0,
            discipline_violations=0,
            ending_cash=509400.0,
        )
        assert "SESSION SUMMARY" in summary
        assert "66.7%" in summary
        assert "9,400.00" in summary

        # 3. Test telegram_notifier module re-exports
        import veterandesk.alerts.telegram_notifier as tn
        assert tn.TelegramService is TelegramService
        assert tn.telegram_service is not None

        # 4. Direct dispatch calls in offline mode
        svc_offline = TelegramService(enabled=False)
        with patch.object(svc_offline, "_persist_message_state"):
            assert svc_offline.send_message("Direct test alert", reference_id="REF_DIR") is True
            assert svc_offline.send_level_hit_alert("OGDC", "TRD_1", "TARGET_HIT", 100.0, 100.0, 500.0) is True
            assert svc_offline.send_graduation_alert("GRADUATED", 30, 60.0, 1500.0, 4.0, "Approved") is True
            assert svc_offline.send_system_health_alert("SYSTEM_DOWN", "Outage", ["database"]) is True
            assert svc_offline.send_daily_brief("2026-09-06", "Open green", []) is True
            assert svc_offline.send_session_summary("2026-09-06", 1, 1, 0, 1000.0, 50.0, 950.0) is True

            # 5. process_queue_sync
            svc_offline.enqueue_message(MessageType.ALERT, "Queue Sync")
            assert svc_offline.process_queue_sync() == 7



    @pytest.mark.asyncio
    async def test_telegram_async_delivery_and_retries(self):
        svc = TelegramService(bot_token="fake_token", chat_id="12345", enabled=True)
        msg = svc.enqueue_message(MessageType.ALERT, "Test Alert")

        # Mock httpx response success
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True, "result": {"message_id": 999}}
            mock_post.return_value = mock_resp

            count = await svc.process_queue()
            assert count == 1
            assert msg.is_delivered is True
            assert len(svc.delivered_history) == 1

        # Mock failure and retry queue
        svc.enabled = True
        msg_fail = svc.enqueue_message(MessageType.ALERT, "Fail Alert")
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Network down")), patch("asyncio.sleep"):
            count = await svc.process_queue()
            assert count == 0
            assert msg_fail.is_delivered is False
            assert msg_fail.attempts == 3

    def test_api_trade_and_journal_endpoints(self):
        client = TestClient(app)

        # 1. get_open_trades
        resp_open = client.get("/trades/open")
        assert resp_open.status_code == 200
        assert isinstance(resp_open.json(), list)

        # 2. get_closed_trades
        resp_closed = client.get("/trades/closed")
        assert resp_closed.status_code == 200
        assert isinstance(resp_closed.json(), list)

        # 3. get_journal
        resp_j = client.get("/journal")
        assert resp_j.status_code == 200
        assert isinstance(resp_j.json(), list)

        # 4. get_lessons
        resp_l = client.get("/lessons")
        assert resp_l.status_code == 200
        assert isinstance(resp_l.json(), list)

        # 5. execute trade pipeline (should pass through Risk Engine)
        payload = {
            "ticker": "OGDC",
            "action": "BUY",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_price": 108.0,
            "twenty_day_adv": 5000000.0,
            "confidence_pct": 75,
            "invalidation_reason": "Breakout invalidated",
        }
        resp_exec = client.post("/trades/execute", json=payload)
        # Depending on risk state, either 200 or 422
        assert resp_exec.status_code in (200, 422)

    def test_migration_runner_basic(self, tmp_path):
        # Test non-existent file
        assert run_migration("sql/non_existent.sql") is False

        # Test valid schema file execution
        dummy_sql = tmp_path / "dummy_schema.sql"
        dummy_sql.write_text("CREATE TABLE test_cov (id INT PRIMARY KEY);")
        with patch("veterandesk.config.settings.database_url", f"sqlite:///{tmp_path}/test_cov.db"):
            assert run_migration(str(dummy_sql)) is True

    @pytest.mark.asyncio
    async def test_post_mortem_engine_edge_cases(self):
        engine = PostMortemEngine()

        # 1. Parse LLM response with malformed JSON
        rec = JournalRecord(
            trade_id="TRD_MALFORMED",
            ticker="OGDC",
            entry_rationale="test",
            exit_rationale="test",
            market_conditions={},
            net_pnl=100.0,
        )
        assert engine._parse_and_apply_llm_response(rec, "Not JSON at all") is False

        # 2. Parse LLM response with invalid verdict
        invalid_json = json.dumps({
            "verdict": "MAYBE",
            "analysis": "Unsure",
            "transferable_lesson": "None",
        })
        assert engine._parse_and_apply_llm_response(rec, invalid_json) is False

        # 3. Mock Anthropic 200 response
        valid_json_text = json.dumps({
            "verdict": "Right",
            "analysis": "Flawless trade.",
            "transferable_lesson": "Follow rules.",
        })
        with patch("veterandesk.config.settings.anthropic_api_key", "sk-ant-test"):
            with patch("veterandesk.config.settings.use_mock_llm_if_no_key", False):
                with patch("httpx.AsyncClient.post") as mock_llm_post:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {
                        "content": [{"text": valid_json_text}]
                    }
                    mock_llm_post.return_value = mock_resp

                    success = await engine._generate_post_mortem(rec)
                    assert success is True
                    assert rec.verdict == TradeVerdict.RIGHT

    def test_paper_broker_invalid_buy_conditions(self):
        ledger = DoubleEntryLedger(starting_balance_pkr=1000.0)
        broker = PaperBroker(ledger=ledger, persist_to_db=False)

        # 1. Invalid shares count
        sig = TradeSignal(
            signal_id="SIG_VALID",
            ticker="OGDC",
            entry_price=100.0,
            stop_loss=95.0,
            target_price=110.0,
            reward_risk_ratio=1.5,
            position_size=10,
            confidence_pct=60,
            invalidation_reason="test",
            created_at=datetime.now(timezone.utc),
            session_id="s",
        )
        with pytest.raises(ValueError, match="Invalid shares count"):
            broker.execute_buy(sig, shares=0, scraped_price=100.0)

        # 2. Stop loss >= scraped price
        with pytest.raises(ValueError, match="Stop loss must be strictly below entry price"):
            broker.execute_buy(sig, shares=10, scraped_price=94.0)

        # 3. Insufficient cash
        with pytest.raises(ValueError, match="Insufficient funds"):
            broker.execute_buy(sig, shares=100, scraped_price=100.0)

    def test_scheduler_and_orb_alert_coverage(self):
        from veterandesk.alerts.scheduler import run_daily_brief_job, run_session_summary_job
        from veterandesk.alerts.telegram import telegram_service
        from veterandesk.market_data.candle_builder import Candle
        from veterandesk.strategy.orb import compute_orb_signal

        # Scheduler failure handling
        with patch.object(telegram_service, "send_daily_brief", side_effect=RuntimeError("Simulated brief fail")):
            assert run_daily_brief_job() is False

        with patch.object(telegram_service, "send_session_summary", side_effect=RuntimeError("Simulated summary fail")):
            assert run_session_summary_job() is False

        # ORB notify=True coverage
        base_dt = datetime(2026, 9, 4, 9, 15, tzinfo=timezone.utc)
        candles = []
        for i in range(15):
            candles.append({
                "timestamp": base_dt + timedelta(minutes=i),
                "open": 326.5,
                "high": 328.0,
                "low": 326.0,
                "close": 327.0,
                "volume": 1000.0,
                "data_status": "ok",
            })
        # Breakout candle
        candles.append({
            "timestamp": base_dt + timedelta(minutes=15),
            "open": 327.5,
            "high": 329.0,
            "low": 327.0,
            "close": 328.5,
            "volume": 3000.0,
            "data_status": "ok",
        })

        sig = compute_orb_signal(
            ticker="OGDC",
            candles_1m=candles,
            session_id="sess_cov",
            notify=True,
        )
        assert sig is not None
        assert sig.ticker == "OGDC"

