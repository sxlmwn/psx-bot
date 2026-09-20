"""
Trade Journal and Groq Post-Mortem Module.

Enforces:
1. Strict schema validation for post-mortems.
2. The 4 non-negotiable verdicts:
   - 'Right'
   - 'Wrong'
   - 'Right-for-wrong-reason'
   - 'Wrong-for-right-reason'
3. Outbound retry queue for Groq / LLM API calls.
4. Immutable original post-mortem records.
"""

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass, field

# Maximum tokens for Groq API calls
# Observed completions: ~364-465 tokens (including ~270-340 reasoning tokens for gpt-oss models)
# qwen/qwen3.8-27b 429s without max_tokens (OTPM limit 1000), works with 800
# 1000 provides 2x headroom for all models
GROQ_MAX_TOKENS = 1000
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from groq import Groq

from veterandesk.config import settings, get_secret
from veterandesk.execution.paper_broker import DemoTrade, TradeStatus, ExitReason
from veterandesk.journal.lessons import LessonsMemory
from veterandesk.journal.recovery import build_recovery_trades
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.post_mortem")


class TradeVerdict(str, Enum):
    RIGHT = "Right"
    WRONG = "Wrong"
    RIGHT_FOR_WRONG_REASON = "Right-for-wrong-reason"
    WRONG_FOR_RIGHT_REASON = "Wrong-for-right-reason"


class PostMortemStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class JournalRecord:
    trade_id: str
    ticker: str
    entry_rationale: str
    exit_rationale: str
    market_conditions: Dict[str, Any]
    verdict: Optional[TradeVerdict] = None
    post_mortem_analysis: Optional[str] = None
    transferable_lesson: Optional[str] = None
    user_annotation: Optional[str] = None
    status: PostMortemStatus = PostMortemStatus.PENDING
    retry_count: int = 0
    net_pnl: float = 0.0
    exit_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    generation_source: Optional[str] = None  # 'llm_primary', 'llm_fallback', 'deterministic_fallback'
    model_used: Optional[str] = None  # Actual model name used
    data_quality_flag: str = "VALID"  # 'VALID' or 'INVALID' - tracks if trade data is stale


class PostMortemEngine:
    """
    Handles LLM reasoning for closed trade evaluation and lesson extraction.
    """

    def __init__(self, lessons_memory: Optional[LessonsMemory] = None, persist_to_db: bool = True, enable_recovery: bool = True) -> None:
        self.lessons_memory = lessons_memory or LessonsMemory()
        self.persist_to_db: bool = persist_to_db
        self.enable_recovery: bool = enable_recovery
        self.pending_queue: List[JournalRecord] = []
        self.completed_journal: Dict[str, JournalRecord] = {}
        self._in_flight: Dict[str, JournalRecord] = {}  # Track trades currently being processed with their records
        self._queue_lock = threading.Lock()  # Protect pending_queue from concurrent access
        self._persistence_alerts_sent: set[str] = set()  # Rate-limit persistence failure alerts per trade_id
        self._startup_complete: bool = False  # Track if startup() has been called
        # __init__ is now pure - no DB, no Groq, no network calls
        # Model validation and recovery moved to explicit startup() method

    def _is_test_environment(self) -> bool:
        """
        Check if running in test environment (pytest).
        
        ONLY blocks writes when pytest is actually running.
        Does NOT silently disable production if ENVIRONMENT is unset or different.
        """
        return (
            "pytest" in sys.modules or
            os.environ.get("PYTEST_CURRENT_TEST") is not None
        )

    def startup(self) -> None:
        """
        Explicit startup method that performs DB recovery and Groq model validation.
        Called from FastAPI startup event in a background thread with timeouts.
        This ensures __init__ is pure and import-time side effects are eliminated.
        """
        if self._startup_complete:
            return
        
        if self.persist_to_db:
            self._validate_groq_models_on_startup()
            if self.enable_recovery:
                self._recover_pending_from_db()
        
        self._startup_complete = True

    def _validate_groq_models_on_startup(self) -> None:
        """Validate that configured Groq models exist and are accessible."""
        groq_api_key = get_secret("GROQ_API_KEY", settings.groq_api_key)
        if not groq_api_key:
            logger.warning("groq_api_key_missing_at_startup", msg="No API key configured, post-mortems will use deterministic fallback")
            return

        try:
            from groq import Groq
            client = Groq(api_key=groq_api_key)
            models = client.models.list()
            available_model_ids = {model.id for model in models.data}
            
            # Check primary model
            if settings.groq_model not in available_model_ids:
                logger.error(
                    "groq_primary_model_not_found",
                    configured_model=settings.groq_model,
                    available_models=list(available_model_ids),
                    msg="Primary Groq model does not exist, will fallback or use deterministic"
                )
            else:
                logger.info("groq_primary_model_valid", model=settings.groq_model)
            
            # Check fallback model
            if settings.groq_fallback_model not in available_model_ids:
                logger.error(
                    "groq_fallback_model_not_found",
                    configured_model=settings.groq_fallback_model,
                    available_models=list(available_model_ids),
                    msg="Fallback Groq model does not exist, only primary model will be used"
                )
            else:
                logger.info("groq_fallback_model_valid", model=settings.groq_fallback_model)
                
        except Exception as e:
            logger.error("groq_model_validation_failed", error=str(e), error_type=type(e).__name__)

    def _recover_pending_from_db(self) -> None:
        """
        Recover pending/failed journal records from database on startup.
        Also finds closed trades with no journal row and queues them.
        This handles restart recovery and backfill of missing post-mortems.
        """
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            
            # Recover PENDING and FAILED (with retries left) records (bounded)
            res = client.table("trade_journal").select("*").in_("post_mortem_status", ["PENDING", "FAILED"]).limit(100).execute()
            recovered_count = 0
            for row in (res.data or []):
                trade_id = row.get("trade_id")
                retry_count = row.get("retry_count", 0)
                status = row.get("post_mortem_status")
                
                # Skip if already in pending queue (avoid duplicates)
                if any(r.trade_id == trade_id for r in self.pending_queue):
                    continue
                    
                # Skip FAILED records that have exceeded max retries
                if status == "FAILED" and retry_count >= 5:
                    continue
                    
                # Reconstruct JournalRecord from DB row
                record = JournalRecord(
                    trade_id=trade_id,
                    ticker=row.get("ticker", "UNKNOWN"),
                    entry_rationale=row.get("entry_rationale", ""),
                    exit_rationale=row.get("exit_rationale", ""),
                    market_conditions=row.get("market_conditions", {}),
                    verdict=TradeVerdict(row.get("verdict")) if row.get("verdict") else None,
                    post_mortem_analysis=row.get("post_mortem_analysis"),
                    transferable_lesson=row.get("transferable_lesson"),
                    user_annotation=row.get("user_annotation"),
                    status=PostMortemStatus(status),
                    retry_count=retry_count,
                    net_pnl=row.get("net_pnl", 0.0),
                    exit_reason=row.get("exit_reason"),
                    created_at=datetime.fromisoformat(row.get("created_at")) if row.get("created_at") else datetime.now(timezone.utc),
                    completed_at=datetime.fromisoformat(row.get("completed_at")) if row.get("completed_at") else None,
                    generation_source=row.get("generation_source"),
                    model_used=row.get("model_used"),
                    data_quality_flag=row.get("data_quality_flag", "VALID"),
                )
                self.pending_queue.append(record)
                recovered_count += 1
            
            if recovered_count > 0:
                logger.info("post_mortem_pending_recovered_from_db", count=recovered_count)
            
            # Find closed trades with no journal row (backfill)
            # IMPORTANT: Only picks trades with status=CLOSED to avoid test rows
            # Limit to recent trades to avoid unbounded recovery
            # Use demo_trades to satisfy FK constraint in lessons_memory
            journal_res = client.table("trade_journal").select("trade_id").execute()
            journal_trade_ids = {row.get("trade_id") for row in (journal_res.data or [])}
            
            # Fetch all CLOSED trades (limit 100)
            trades_res = client.table("demo_trades").select("*").eq("status", "CLOSED").limit(100).execute()
            
            # Filter to trades without journal rows
            trades_to_recover = [
                row.get("trade_id") for row in (trades_res.data or [])
                if row.get("trade_id") and row.get("trade_id") not in journal_trade_ids
            ]
            
            if trades_to_recover:
                # Use build_recovery_trades to reconstruct trades
                reconstructed_trades = build_recovery_trades(client, trade_ids=trades_to_recover)
                
                # Queue each reconstructed trade for post-mortem
                backfill_count = 0
                for trade in reconstructed_trades:
                    self.queue_trade_for_post_mortem(trade)
                    backfill_count += 1
                
                if backfill_count > 0:
                    logger.info("post_mortem_backfill_queued", count=backfill_count)
                
        except Exception as e:
            logger.warning("post_mortem_db_recovery_failed", error=str(e), error_type=type(e).__name__)
            # Don't crash worker startup on recovery failure
            logger.info("post_mortem_recovery_skipped_startup_continues")

    def queue_trade_for_post_mortem(
        self,
        trade: DemoTrade,
        market_conditions: Optional[Dict[str, Any]] = None
    ) -> JournalRecord:
        """Queue closed trade for post-mortem processing. Idempotent: same trade_id queued twice = one record."""
        with self._queue_lock:
            # Check if already in pending queue (idempotency)
            if any(r.trade_id == trade.trade_id for r in self.pending_queue):
                logger.info("post_mortem_already_queued", trade_id=trade.trade_id, ticker=trade.ticker)
                return next(r for r in self.pending_queue if r.trade_id == trade.trade_id)
            
            # Check if already in-flight (idempotency)
            if trade.trade_id in self._in_flight:
                logger.info("post_mortem_already_in_flight", trade_id=trade.trade_id, ticker=trade.ticker)
                # Return the actual in-flight record (never create placeholders)
                return self._in_flight[trade.trade_id]
            
            # Check if already completed (idempotency)
            if trade.trade_id in self.completed_journal:
                logger.info("post_mortem_already_completed", trade_id=trade.trade_id, ticker=trade.ticker)
                return self.completed_journal[trade.trade_id]
            
            # Use empty dict if market_conditions not provided (no fabricated data)
            conditions = market_conditions or {}

            # Build entry description - prices in demo_trades are FILL prices, not market prices
            # The slippage_pct represents the slippage that was applied to get the fill
            entry_desc = (
                f"ORB breakout buy filled at PKR {trade.filled_entry_price:.2f} "
                f"({trade.slippage_pct*100:.2f}% slippage applied) with stop at PKR {trade.stop_loss:.2f}"
            )
            
            # Build exit description - prices in demo_trades are FILL prices, not market prices
            reason_val = trade.exit_reason.value if trade.exit_reason else "UNKNOWN"
            exit_desc = (
                f"Closed via {reason_val} at PKR {trade.filled_exit_price:.2f} "
                f"({trade.slippage_pct*100:.2f}% slippage applied)"
            )
            
            # Add data quality warning for INVALID trades
            data_quality_flag = getattr(trade, 'data_quality_flag', 'VALID')
            data_quality_warning = ""
            if data_quality_flag == "INVALID":
                data_quality_warning = " [WARNING: Analysis based on stale candle data]"
                exit_desc += data_quality_warning

            record = JournalRecord(
                trade_id=trade.trade_id,
                ticker=trade.ticker,
                entry_rationale=entry_desc,
                exit_rationale=exit_desc,
                market_conditions=conditions,
                status=PostMortemStatus.PENDING,
                retry_count=0,
                net_pnl=trade.net_pnl,
                exit_reason=reason_val,
                data_quality_flag=data_quality_flag,
            )
            self.pending_queue.append(record)
            self._persist_journal_record(record)
            logger.info("post_mortem_queued", trade_id=trade.trade_id, ticker=trade.ticker)
            return record

    async def process_pending_queue(self) -> int:
        """
        Process pending trades in the queue.
        Retries failed LLM calls; never silently drops a trade.
        Thread-safe: locks pending_queue during processing.
        """
        with self._queue_lock:
            if not self.pending_queue:
                return 0

            # Make a copy to process while holding the lock briefly
            queue_copy = self.pending_queue.copy()
            self.pending_queue.clear()
            # Mark all as in-flight with their actual records
            for record in queue_copy:
                self._in_flight[record.trade_id] = record

        processed = 0
        remaining: List[JournalRecord] = []

        for record in queue_copy:
            try:
                success = await self._generate_post_mortem(record)
                if success:
                    record.status = PostMortemStatus.COMPLETED
                    record.completed_at = datetime.now(timezone.utc)
                    self.completed_journal[record.trade_id] = record
                    self._persist_journal_record(record)
                    processed += 1
                else:
                    # Handle failure with retry cap
                    record.retry_count += 1
                    self._handle_retry_failure(record, remaining)
            except Exception as e:
                # Handle exception with retry cap
                record.retry_count += 1
                logger.error("post_mortem_error", trade_id=record.trade_id, error=str(e))
                self._handle_retry_failure(record, remaining)

        # Add remaining back to queue and clear in-flight with lock
        with self._queue_lock:
            self.pending_queue.extend(remaining)
            # Clear in-flight for completed/remaining trades
            for record in queue_copy:
                self._in_flight.pop(record.trade_id, None)
        
        return processed

    def _handle_retry_failure(self, record: JournalRecord, remaining: List[JournalRecord]) -> None:
        """
        Shared retry logic for both explicit failures and exceptions.
        Applies retry cap: >=5 -> FAILED (persisted once, ONE alert, not re-queued); <5 -> re-queued.
        """
        if record.retry_count >= 5:
            record.status = PostMortemStatus.FAILED
            logger.error("post_mortem_max_retries_exceeded", trade_id=record.trade_id)
            # Send alert for failed post-mortem (once, then do NOT re-queue)
            try:
                from veterandesk.alerts.telegram import telegram_service
                telegram_service.send_message(
                    f"⚠️ Post-Mortem Failed: Trade {record.trade_id} ({record.ticker}) failed post-mortem after 5 retries."
                )
            except Exception as alert_ex:
                logger.warning("post_mortem_failed_alert_failed", error=str(alert_ex))
            self._persist_journal_record(record)
            # FAILED records are persisted but NOT re-queued
        else:
            # retry_count < 5: re-queue for retry
            self._persist_journal_record(record)
            remaining.append(record)

    def _persist_journal_record(self, record: JournalRecord) -> None:
        """Persist trade journal and verdict to live Supabase PostgreSQL."""
        # Hard safety guard: no DB writes in test environments
        if self._is_test_environment():
            logger.warning("journal_persistence_blocked_test_environment", trade_id=record.trade_id)
            return
            
        if not self.persist_to_db:
            # Safety check: if we're about to skip a write outside of pytest, alert loudly
            # This catches cases where persist_to_db is False in production by mistake
            if not self._is_test_environment():
                logger.error(
                    "journal_persistence_disabled_in_production",
                    trade_id=record.trade_id,
                    warning="DB writes are disabled (persist_to_db=False) outside of test environment. This may be a configuration error."
                )
                try:
                    from veterandesk.alerts.telegram import telegram_service
                    telegram_service.send_message(
                        f"⚠️ Journal Persistence Disabled: DB writes are disabled (persist_to_db=False) for trade {record.trade_id} ({record.ticker}) outside of test environment. Check configuration."
                    )
                except Exception as alert_ex:
                    logger.warning("journal_persistence_disabled_alert_failed", error=str(alert_ex))
            return  # Skip persistence when explicitly disabled
            
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            row = {
                "trade_id": record.trade_id,
                "market_conditions": record.market_conditions,
                "entry_rationale": record.entry_rationale,
                "exit_rationale": record.exit_rationale,
                "verdict": record.verdict.value if record.verdict else None,
                "post_mortem_status": record.status.value,
                "post_mortem_analysis": record.post_mortem_analysis,
                "transferable_lesson": record.transferable_lesson,
                "user_annotation": record.user_annotation,
                "retry_count": record.retry_count,
                "created_at": record.created_at.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "generation_source": record.generation_source,
                "model_used": record.model_used,
                "data_quality_flag": record.data_quality_flag,
            }
            client.table("trade_journal").upsert(row, on_conflict="trade_id").execute()
            if record.status == PostMortemStatus.COMPLETED and record.transferable_lesson:
                # Skip active lessons for INVALID trades (stale candle data)
                if record.data_quality_flag == "INVALID":
                    logger.info(
                        "lesson_skipped_invalid_data_quality",
                        trade_id=record.trade_id,
                        reason="Trade has INVALID data quality flag (stale candle data), lesson not activated"
                    )
                else:
                    # Check-then-insert to avoid duplicates (no UNIQUE constraint on trade_id)
                    existing_lesson = client.table("lessons_memory").select("*").eq("trade_id", record.trade_id).execute()
                    if not existing_lesson.data:
                        client.table("lessons_memory").insert({
                            "trade_id": record.trade_id,
                            "category": f"ORB_{record.ticker}",
                            "lesson_text": record.transferable_lesson,
                            "is_active": True,
                            "times_cited": 0,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
            logger.info("journal_persisted_to_supabase", trade_id=record.trade_id, status=record.status.value, generation_source=record.generation_source, data_quality=record.data_quality_flag)
        except Exception as e:
            logger.error("journal_db_persistence_failed", trade_id=record.trade_id, error=str(e), error_type=type(e).__name__)
            # Send alert for failed journal persistence (rate-limited to once per trade_id)
            if record.trade_id not in self._persistence_alerts_sent:
                try:
                    from veterandesk.alerts.telegram import telegram_service
                    telegram_service.send_message(
                        f"⚠️ Journal Persistence Failed: Failed to persist trade journal for {record.trade_id} ({record.ticker}). Error: {str(e)}"
                    )
                    self._persistence_alerts_sent.add(record.trade_id)
                except Exception as alert_ex:
                    logger.warning("journal_persistence_alert_failed", error=str(alert_ex))

    async def _generate_post_mortem(self, record: JournalRecord) -> bool:
        """
        Query Groq API (or deterministic fallback) to produce structured verdict.
        Uses model 'openai/gpt-oss-120b' with fallback to 'qwen/qwen3.8-27b'.
        """
        groq_api_key = get_secret("GROQ_API_KEY", settings.groq_api_key)
        if not groq_api_key or settings.use_mock_llm_if_no_key:
            logger.warning("groq_api_key_missing_using_deterministic_fallback", trade_id=record.trade_id)
            return self._generate_deterministic_fallback(record)

        # Get relevant past lessons for this ticker
        lesson_context = self.lessons_memory.build_post_mortem_lesson_context(record.ticker)

        # Increment times_cited for relevant lessons
        relevant_lessons = self.lessons_memory.get_lessons_for_ticker(record.ticker)
        for lesson in relevant_lessons:
            self.lessons_memory.cite_lesson(lesson)

        # Real Groq API Call
        # Add stale candle data warning for INVALID trades
        data_quality_warning = ""
        if record.data_quality_flag == "INVALID":
            data_quality_warning = "CRITICAL: This trade is based on STALE CANDLE DATA. The analysis may be unreliable.\n\n"
        
        # Format market conditions - use "not recorded" if empty
        if record.market_conditions:
            conditions_str = json.dumps(record.market_conditions)
        else:
            conditions_str = "not recorded"
        
        prompt = (
            f"You are the disciplined chief risk officer for a PSX trading desk. Analyze this trade:\n"
            f"Ticker: {record.ticker}\n"
            f"Entry: {record.entry_rationale}\n"
            f"Exit: {record.exit_rationale}\n"
            f"Net PnL: PKR {record.net_pnl:+,.2f}\n"
            f"Exit Reason: {record.exit_reason or 'UNKNOWN'}\n"
            f"Market conditions: {conditions_str}\n"
            f"IMPORTANT: The prices shown are fill prices (including slippage). Pre-slippage market prices are not stored in the database.\n\n"
            f"{data_quality_warning}"
            f"{lesson_context}\n\n"
            f"CRITICAL DISCIPLINE RULES FOR VERDICTS:\n"
            f"1. A trade with Net PnL <= 0 must NEVER be called 'Right' and must NEVER be described as having 'positive expectancy'.\n"
            f"2. If price hit target nominally but Net PnL was negative due to commissions and slippage, classify as 'Right-for-wrong-reason' or 'Wrong-for-right-reason', explaining inadequate friction margin.\n"
            f"3. If stop loss was hit cleanly according to discipline, classify as 'Wrong-for-right-reason'.\n"
            f"4. If closed at 15:20 PKT cutoff: 'Right' if net profit, 'Wrong-for-right-reason' if net loss.\n"
            f"5. Only trades with net positive PnL that followed the plan can be classified as 'Right'.\n\n"
            f"Respond ONLY with a JSON object containing:\n"
            f'{{"verdict": "Right" | "Wrong" | "Right-for-wrong-reason" | "Wrong-for-right-reason",\n'
            f' "analysis": "2-3 concise sentences analyzing execution vs plan",\n'
            f' "transferable_lesson": "One general rule/lesson to apply to future trades (must NEVER claim positive expectancy on a net loss)"}}\n'
        )

        def _call_groq(model_name: str) -> Optional[str]:
            client = Groq(api_key=groq_api_key)
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are the chief risk officer for a quantitative PSX trading desk. "
                            "You evaluate closed trades with strict mathematical discipline and return only valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=GROQ_MAX_TOKENS,
            )
            return completion.choices[0].message.content

        content: Optional[str] = None
        successful_model: Optional[str] = None
        generation_source: Optional[str] = None
        
        models_to_try = [settings.groq_model, settings.groq_fallback_model]
        for idx, candidate_model in enumerate(models_to_try):
            try:
                content = await asyncio.to_thread(_call_groq, candidate_model)
                if content:
                    # Validate JSON and verdict before treating as success
                    try:
                        # Quick validation: can we parse it?
                        start_idx = content.find("{")
                        end_idx = content.rfind("}")
                        if start_idx == -1 or end_idx == -1:
                            logger.warning("groq_invalid_json_no_braces", model=candidate_model, trade_id=record.trade_id)
                            content = None  # Treat as failure, try next model
                            continue
                        json_str = content[start_idx : end_idx + 1]
                        data = json.loads(json_str)

                        # Validate verdict field (reuse validation from _parse_and_apply_llm_response)
                        verdict_str = data.get("verdict")
                        if verdict_str not in [v.value for v in TradeVerdict]:
                            logger.warning("groq_invalid_verdict", model=candidate_model, trade_id=record.trade_id, verdict=verdict_str)
                            content = None  # Treat as failure, try next model
                            continue

                        # JSON and verdict are valid, this model succeeded
                        successful_model = candidate_model
                        generation_source = "llm_primary" if idx == 0 else "llm_fallback"
                        logger.info("groq_post_mortem_success", model=candidate_model, trade_id=record.trade_id, generation_source=generation_source)
                        break
                    except json.JSONDecodeError as e:
                        logger.warning("groq_invalid_json_parse_error", model=candidate_model, trade_id=record.trade_id, error=str(e))
                        content = None  # Treat as failure, try next model
                        continue
            except Exception as e:
                logger.warning("groq_post_mortem_attempt_failed", model=candidate_model, error=str(e), error_type=type(e).__name__)

        if not content:
            logger.error("groq_all_models_failed_fallback_to_deterministic", trade_id=record.trade_id)
            # Send alert for fallback to deterministic
            try:
                from veterandesk.alerts.telegram import telegram_service
                telegram_service.send_message(
                    f"⚠️ Post-Mortem Fallback: Both Groq models failed for trade {record.trade_id} ({record.ticker}). Using deterministic fallback."
                )
            except Exception as alert_ex:
                logger.warning("post_mortem_fallback_alert_failed", error=str(alert_ex))
            return self._generate_deterministic_fallback(record)

        # Track which model was used
        record.generation_source = generation_source
        record.model_used = successful_model
        return self._parse_and_apply_llm_response(record, content)

    def _generate_deterministic_fallback(self, record: JournalRecord) -> bool:
        """
        Deterministic, rule-based fallback post-mortem generator.
        Ensures system functions 100% reliably even when offline.

        Strict rules:
        - A trade with Net PnL <= 0 must NEVER be labeled 'Right' and must NEVER claim 'positive expectancy'.
        - If target was nominally reached but net PnL is negative (due to commissions + slippage exceeding gross move),
          verdict MUST be 'Right-for-wrong-reason' with an analysis highlighting inadequate friction margin.
        - If stopped out according to discipline, verdict is 'Wrong-for-right-reason'.
        - If closed at 15:20 cutoff: 'Right' if profitable, 'Wrong-for-right-reason' if loss.
        """
        is_net_profit = record.net_pnl > 0
        hit_stop = record.exit_reason == "STOP_HIT" or "STOP_HIT" in record.exit_rationale
        hit_target = record.exit_reason == "TARGET_HIT" or "TARGET_HIT" in record.exit_rationale
        hit_time_stop = record.exit_reason == "TIME_STOP_1520" or "TIME_STOP_1520" in record.exit_rationale

        if hit_target:
            if is_net_profit:
                verdict = TradeVerdict.RIGHT
                analysis = (
                    f"Trade followed the ORB breakout plan accurately in {record.ticker}. "
                    f"Target was reached and produced net profit of PKR {record.net_pnl:+,.2f} "
                    f"after absorbing round-trip slippage and brokerage fees."
                )
                lesson = (
                    f"In strong momentum breakouts for {record.ticker}, allowing price to reach "
                    "full target with sufficient friction buffer yields positive expectancy."
                )
            else:
                verdict = TradeVerdict.RIGHT_FOR_WRONG_REASON
                analysis = (
                    f"Trade nominally hit target in {record.ticker}, but produced a net loss of "
                    f"PKR {record.net_pnl:+,.2f} because round-trip transaction friction (brokerage commissions "
                    "and slippage) exceeded the gross move. The profit target buffer was too narrow to overcome execution costs."
                )
                lesson = (
                    "Profit target distance must substantially exceed round-trip execution friction "
                    "(brokerage commissions and slippage); avoid narrow targets where transaction costs consume the entire move."
                )
        elif hit_stop:
            verdict = TradeVerdict.WRONG_FOR_RIGHT_REASON
            analysis = (
                f"Trade hit stop loss as planned in {record.ticker} (Net PnL: PKR {record.net_pnl:+,.2f}). "
                "The setup complied with all ORB rules, but market reversed into the opening range. "
                "Capital was protected by disciplined stop enforcement."
            )
            lesson = (
                "Taking a planned stop loss protects capital and proves disciplined execution; "
                "controlled losses are regular business costs."
            )
        elif hit_time_stop:
            if is_net_profit:
                verdict = TradeVerdict.RIGHT
                analysis = (
                    f"Trade closed at mandatory 15:20 PKT session cutoff in {record.ticker} "
                    f"with net profit of PKR {record.net_pnl:+,.2f}. Discipline maintained with zero overnight risk."
                )
                lesson = "Mandatory intraday flat rules protect against overnight gap risk while locking in session gains."
            else:
                verdict = TradeVerdict.WRONG_FOR_RIGHT_REASON
                analysis = (
                    f"Trade closed at mandatory 15:20 PKT session cutoff in {record.ticker} "
                    f"with net loss of PKR {record.net_pnl:+,.2f}. Flat discipline was honored, avoiding unhedged overnight exposure."
                )
                lesson = "Intraday discipline requires exiting at 15:20 PKT regardless of PnL to prevent unhedged overnight risk."
        else:
            if is_net_profit:
                verdict = TradeVerdict.RIGHT
                analysis = f"Position closed with net profit of PKR {record.net_pnl:+,.2f} on {record.ticker}."
                lesson = "Adhering to trade rules and risk parameters ensures sustainable execution."
            else:
                verdict = TradeVerdict.WRONG
                analysis = f"Position closed with net loss of PKR {record.net_pnl:+,.2f} on {record.ticker}."
                lesson = "Review setup criteria and fee drag before committing capital to marginal setups."

        record.verdict = verdict
        record.post_mortem_analysis = analysis
        record.transferable_lesson = lesson
        record.generation_source = "deterministic_fallback"
        record.model_used = None

        # Register lesson in memory (skip for INVALID trades)
        if record.data_quality_flag != "INVALID":
            self.lessons_memory.add_lesson(
                category=f"ORB_{record.ticker}",
                text=lesson,
                trade_id=record.trade_id,
            )
        else:
            logger.info(
                "deterministic_lesson_skipped_invalid_data_quality",
                trade_id=record.trade_id,
                reason="Trade has INVALID data quality flag, lesson not added to memory"
            )
        return True

    def _parse_and_apply_llm_response(self, record: JournalRecord, response_text: str) -> bool:
        """Parse and strictly validate LLM JSON response."""
        try:
            start_idx = response_text.find("{")
            end_idx = response_text.rfind("}")
            if start_idx == -1 or end_idx == -1:
                return False
            json_str = response_text[start_idx : end_idx + 1]
            data = json.loads(json_str)

            verdict_str = data.get("verdict")
            if verdict_str not in [v.value for v in TradeVerdict]:
                logger.error("invalid_verdict_returned", verdict=verdict_str)
                return False

            parsed_verdict = TradeVerdict(verdict_str)
            parsed_analysis = data.get("analysis", "").strip()
            parsed_lesson = data.get("transferable_lesson", "").strip()

            # Enforce hard invariant: Net loss trades cannot be 'Right'
            if record.net_pnl <= 0 and parsed_verdict == TradeVerdict.RIGHT:
                logger.warning("overriding_contradictory_verdict", trade_id=record.trade_id, old="Right", new="Right-for-wrong-reason")
                parsed_verdict = TradeVerdict.RIGHT_FOR_WRONG_REASON

            # Enforce hard invariant: Net loss trades cannot claim positive expectancy
            if record.net_pnl <= 0 and parsed_lesson and "positive expectancy" in parsed_lesson.lower():
                parsed_lesson = (
                    "Profit target distance must substantially exceed round-trip execution friction "
                    "(brokerage commissions and slippage); avoid narrow targets where transaction costs consume the entire move."
                )

            record.verdict = parsed_verdict
            record.post_mortem_analysis = parsed_analysis
            record.transferable_lesson = parsed_lesson

            # Register lesson in memory (skip for INVALID trades)
            if record.transferable_lesson and record.data_quality_flag != "INVALID":
                self.lessons_memory.add_lesson(
                    category=f"ORB_{record.ticker}",
                    text=record.transferable_lesson,
                    trade_id=record.trade_id,
                )
            elif record.transferable_lesson and record.data_quality_flag == "INVALID":
                logger.info(
                    "llm_lesson_skipped_invalid_data_quality",
                    trade_id=record.trade_id,
                    reason="Trade has INVALID data quality flag, LLM lesson not added to memory"
                )
            return True
        except Exception as e:
            logger.error("llm_parse_error", error=str(e), raw=response_text)
            return False
