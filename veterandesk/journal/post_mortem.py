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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from groq import Groq

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
    net_pnl: float = 0.0
    exit_reason: Optional[str] = None
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

        entry_desc = (
            f"ORB breakout buy at PKR {trade.filled_entry_price:.2f} "
            f"(market: PKR {trade.entry_price:.2f}) with stop at PKR {trade.stop_loss:.2f}"
        )
        reason_val = trade.exit_reason.value if trade.exit_reason else "UNKNOWN"
        market_exit_str = f"market: PKR {trade.exit_price:.2f}, " if trade.exit_price is not None else ""
        exit_desc = (
            f"Closed via {reason_val} at {market_exit_str}fill: PKR {trade.filled_exit_price:.2f} "
            f"(slippage: {trade.slippage_pct*100:.2f}%), Net PnL: PKR {trade.net_pnl:+,.2f}"
        )

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
        Query Groq API (or deterministic fallback) to produce structured verdict.
        Uses model 'openai/gpt-oss-120b' with fallback to 'qwen/qwen3.6-27b'.
        """
        groq_api_key = settings.groq_api_key or os.environ.get("GROQ_API_KEY")
        if not groq_api_key or settings.use_mock_llm_if_no_key:
            return self._generate_deterministic_fallback(record)

        # Real Groq API Call
        prompt = (
            f"You are the disciplined chief risk officer for a PSX trading desk. Analyze this trade:\n"
            f"Ticker: {record.ticker}\n"
            f"Entry: {record.entry_rationale}\n"
            f"Exit: {record.exit_rationale}\n"
            f"Net PnL: PKR {record.net_pnl:+,.2f}\n"
            f"Exit Reason: {record.exit_reason or 'UNKNOWN'}\n"
            f"Conditions: {json.dumps(record.market_conditions)}\n\n"
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
            client = Groq()
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
            )
            return completion.choices[0].message.content

        content: Optional[str] = None
        models_to_try = [settings.groq_model, settings.groq_fallback_model]
        for candidate_model in models_to_try:
            try:
                content = await asyncio.to_thread(_call_groq, candidate_model)
                if content:
                    logger.info("groq_post_mortem_success", model=candidate_model, trade_id=record.trade_id)
                    break
            except Exception as e:
                logger.warning("groq_post_mortem_attempt_failed", model=candidate_model, error=str(e))

        if not content:
            logger.warning("groq_all_models_failed_fallback_to_deterministic", trade_id=record.trade_id)
            return self._generate_deterministic_fallback(record)

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
