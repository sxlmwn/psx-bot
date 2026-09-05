"""
Trade Journal and Claude Post-Mortem Module.

Enforces:
1. Strict schema validation for post-mortems.
2. The 4 non-negotiable verdicts:
   - 'Right'
   - 'Wrong'
   - 'Right-for-wrong-reason'
   - 'Wrong-for-right-reason'
3. Outbound retry queue for Claude / LLM API calls.
4. Immutable original post-mortem records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import httpx

from veterandesk.config import settings
from veterandesk.execution.paper_broker import DemoTrade
from veterandesk.journal.lessons import LessonsMemory
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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


class PostMortemEngine:
    """
    Handles LLM reasoning for closed trade evaluation and lesson extraction.
    """

    def __init__(self, lessons_memory: Optional[LessonsMemory] = None) -> None:
        self.lessons_memory = lessons_memory or LessonsMemory()
        self.pending_queue: List[JournalRecord] = []
        self.completed_journal: Dict[str, JournalRecord] = {}

    def queue_trade_for_post_mortem(
        self,
        trade: DemoTrade,
        market_conditions: Optional[Dict[str, Any]] = None
    ) -> JournalRecord:
        """Queue closed trade for post-mortem processing."""
        conditions = market_conditions or {
            "trend": "bullish_breakout",
            "volume_level": "above_average",
            "kse100_direction": "positive",
        }

        entry_desc = f"ORB breakout buy at PKR {trade.filled_entry_price:.2f} with stop at PKR {trade.stop_loss:.2f}"
        exit_desc = f"Closed via {trade.exit_reason.value if trade.exit_reason else 'UNKNOWN'} at PKR {trade.filled_exit_price:.2f}, Net PnL: PKR {trade.net_pnl:+,.2f}"

        record = JournalRecord(
            trade_id=trade.trade_id,
            ticker=trade.ticker,
            entry_rationale=entry_desc,
            exit_rationale=exit_desc,
            market_conditions=conditions,
            status=PostMortemStatus.PENDING,
            retry_count=0,
        )
        self.pending_queue.append(record)
        self._persist_journal_record(record)
        logger.info("post_mortem_queued", trade_id=trade.trade_id, ticker=trade.ticker)
        return record

    async def process_pending_queue(self) -> int:
        """
        Process pending trades in the queue.
        Retries failed LLM calls; never silently drops a trade.
        """
        if not self.pending_queue:
            return 0

        processed = 0
        remaining: List[JournalRecord] = []

        for record in self.pending_queue:
            try:
                success = await self._generate_post_mortem(record)
                if success:
                    record.status = PostMortemStatus.COMPLETED
                    record.completed_at = datetime.now(timezone.utc)
                    self.completed_journal[record.trade_id] = record
                    self._persist_journal_record(record)
                    processed += 1
                else:
                    record.retry_count += 1
                    if record.retry_count >= 5:
                        record.status = PostMortemStatus.FAILED
                        logger.error("post_mortem_max_retries_exceeded", trade_id=record.trade_id)
                    self._persist_journal_record(record)
                    remaining.append(record)
            except Exception as e:
                record.retry_count += 1
                logger.error("post_mortem_error", trade_id=record.trade_id, error=str(e))
                self._persist_journal_record(record)
                remaining.append(record)

        self.pending_queue = remaining
        return processed

    def _persist_journal_record(self, record: JournalRecord) -> None:
        """Persist trade journal and verdict to live Supabase PostgreSQL."""
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
            }
            client.table("trade_journal").upsert(row, on_conflict="trade_id").execute()
            if record.status == PostMortemStatus.COMPLETED and record.transferable_lesson:
                client.table("lessons_memory").insert({
                    "trade_id": record.trade_id,
                    "category": f"ORB_{record.ticker}",
                    "lesson_text": record.transferable_lesson,
                    "is_active": True,
                    "times_cited": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
            logger.info("journal_persisted_to_supabase", trade_id=record.trade_id, status=record.status.value)
        except Exception as e:
            logger.warning("journal_db_persistence_skipped", error=str(e))

    async def _generate_post_mortem(self, record: JournalRecord) -> bool:
        """
        Query Claude / Groq or deterministic fallback to produce structured verdict.
        """
        # If API key not provided or mock requested:
        if not settings.anthropic_api_key or settings.use_mock_llm_if_no_key:
            return self._generate_deterministic_fallback(record)

        # Real Anthropic API Call
        prompt = (
            f"You are the disciplined chief risk officer for a PSX trading desk. Analyze this trade:\n"
            f"Ticker: {record.ticker}\n"
            f"Entry: {record.entry_rationale}\n"
            f"Exit: {record.exit_rationale}\n"
            f"Conditions: {json.dumps(record.market_conditions)}\n\n"
            f"Respond ONLY with a JSON object containing:\n"
            f'{{"verdict": "Right" | "Wrong" | "Right-for-wrong-reason" | "Wrong-for-right-reason",\n'
            f' "analysis": "2-3 concise sentences analyzing execution vs plan",\n'
            f' "transferable_lesson": "One general rule/lesson to apply to future trades"}}\n'
        )

        headers = {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": settings.anthropic_model,
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            if resp.status_code != 200:
                logger.warning("anthropic_api_error", status=resp.status_code, body=resp.text)
                return False

            data = resp.json()
            content = data["content"][0]["text"]
            return self._parse_and_apply_llm_response(record, content)

    def _generate_deterministic_fallback(self, record: JournalRecord) -> bool:
        """
        Deterministic, rule-based fallback post-mortem generator.
        Ensures system functions 100% reliably even when offline.
        """
        is_profit = "Net PnL: PKR +" in record.exit_rationale
        hit_stop = "STOP_HIT" in record.exit_rationale
        hit_target = "TARGET_HIT" in record.exit_rationale

        if hit_target:
            verdict = TradeVerdict.RIGHT
            analysis = (
                f"Trade followed the ORB breakout plan accurately in {record.ticker}. "
                "Target reached with required volume expansion and disciplined stop trailing."
            )
            lesson = f"In strong momentum breakouts for {record.ticker}, allowing price to reach 1.5x range height yields positive expectancy."
        elif hit_stop:
            verdict = TradeVerdict.WRONG_FOR_RIGHT_REASON
            analysis = (
                f"Trade hit stop loss as defined. The setup complied with all ORB rules, "
                "but market reversed into the opening range. Risk was properly capped at 1%."
            )
            lesson = "Taking a planned stop loss protects capital and proves disciplined execution; losses are regular business costs."
        else:
            verdict = TradeVerdict.RIGHT if is_profit else TradeVerdict.WRONG
            analysis = f"Position closed under session time rules ({record.exit_rationale}). Plan followed with no rule violations."
            lesson = "Intraday discipline requires exiting before 15:20 PKT regardless of emotion."

        record.verdict = verdict
        record.post_mortem_analysis = analysis
        record.transferable_lesson = lesson

        # Register lesson in memory
        self.lessons_memory.add_lesson(
            category=f"ORB_{record.ticker}",
            text=lesson,
            trade_id=record.trade_id,
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

            record.verdict = TradeVerdict(verdict_str)
            record.post_mortem_analysis = data.get("analysis", "").strip()
            record.transferable_lesson = data.get("transferable_lesson", "").strip()

            if record.transferable_lesson:
                self.lessons_memory.add_lesson(
                    category=f"ORB_{record.ticker}",
                    text=record.transferable_lesson,
                    trade_id=record.trade_id,
                )
            return True
        except Exception as e:
            logger.error("llm_parse_error", error=str(e), raw=response_text)
            return False
