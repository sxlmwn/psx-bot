"""
Lessons Memory Module for VeteranDesk.

Stores transferable lessons extracted from post-mortems.
Injects active lessons into session context before each trading day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional
import uuid

from veterandesk.logging import get_logger

logger = get_logger("veterandesk.lessons")


@dataclass
class Lesson:
    id: Any
    trade_id: Optional[str]
    category: str
    lesson_text: str
    is_active: bool = True
    times_cited: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class LessonsMemory:
    """
    Repository of disciplined trading lessons with live Supabase synchronization.
    """

    def __init__(self, sync_with_db: bool = False) -> None:
        self._lessons: List[Lesson] = []
        self.sync_with_db = sync_with_db
        if self.sync_with_db:
            self.load_from_supabase()

    def load_from_supabase(self) -> int:
        """Fetch active lessons from Supabase PostgreSQL."""
        try:
            from veterandesk.database.session import db_manager
            client = db_manager.get_client()
            res = client.table("lessons_memory").select("*").eq("is_active", True).execute()
            rows = res.data or []
            self._lessons = [
                Lesson(
                    id=r["id"],
                    trade_id=r.get("trade_id"),
                    category=r.get("category", "GENERAL"),
                    lesson_text=r.get("lesson_text", "").strip(),
                    is_active=r.get("is_active", True),
                    times_cited=int(r.get("times_cited", 0)),
                )
                for r in rows
            ]
            logger.info("lessons_loaded_from_supabase", count=len(self._lessons))
            return len(self._lessons)
        except Exception as e:
            logger.warning("lessons_db_load_failed", error=str(e))
            return 0

    def add_lesson(self, category: str, text: str, trade_id: Optional[str] = None) -> Lesson:
        lesson = Lesson(
            id=str(uuid.uuid4()),
            trade_id=trade_id,
            category=category,
            lesson_text=text.strip(),
            is_active=True,
            times_cited=0,
            created_at=datetime.now(timezone.utc),
        )
        self._lessons.append(lesson)
        return lesson

    def get_active_lessons(self) -> List[Lesson]:
        if self.sync_with_db and not self._lessons:
            self.load_from_supabase()
        return [l for l in self._lessons if l.is_active]

    def get_lessons_for_ticker(self, ticker: str) -> List[Lesson]:
        sym = ticker.upper().strip()
        active = self.get_active_lessons()
        ticker_specific = [l for l in active if sym in l.category.upper() or sym in l.lesson_text.upper()]
        return ticker_specific if ticker_specific else active

    def cite_lesson(self, lesson: Lesson) -> None:
        """Increment citation counter both in memory and live Supabase."""
        lesson.times_cited += 1
        if self.sync_with_db:
            try:
                from veterandesk.database.session import db_manager
                client = db_manager.get_client()
                client.table("lessons_memory").update({
                    "times_cited": lesson.times_cited
                }).eq("id", lesson.id).execute()
                logger.info("lesson_citation_persisted", lesson_id=lesson.id, times_cited=lesson.times_cited)
            except Exception as e:
                logger.warning("lesson_citation_persist_failed", lesson_id=lesson.id, error=str(e))

    def deactivate_lesson(self, lesson_id: Any) -> bool:
        for l in self._lessons:
            if l.id == lesson_id:
                l.is_active = False
                if self.sync_with_db:
                    try:
                        from veterandesk.database.session import db_manager
                        client = db_manager.get_client()
                        client.table("lessons_memory").update({"is_active": False}).eq("id", lesson_id).execute()
                    except Exception:
                        pass
                return True
        return False

    def build_pre_session_prompt_context(self) -> str:
        """
        Build text block of active lessons to inject into agent context
        before market open each day. Increments times_cited counter.
        """
        active = self.get_active_lessons()
        if not active:
            return "No previous lessons recorded in memory."

        lines = ["=== VETERANDESK ACTIVE LESSONS MEMORY ==="]
        for idx, l in enumerate(active, 1):
            self.cite_lesson(l)
            lines.append(f"{idx}. [{l.category}] {l.lesson_text} (Cited {l.times_cited}x)")
        lines.append("=========================================")
        return "\n".join(lines)

