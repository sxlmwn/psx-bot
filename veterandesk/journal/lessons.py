"""
Lessons Memory Module for VeteranDesk.

Stores transferable lessons extracted from post-mortems.
Injects active lessons into session context before each trading day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
import uuid


@dataclass
class Lesson:
    id: str
    trade_id: Optional[str]
    category: str
    lesson_text: str
    is_active: bool = True
    times_cited: int = 0
    created_at: datetime = datetime.now(timezone.utc)


class LessonsMemory:
    """
    Repository of disciplined trading lessons.
    """

    def __init__(self) -> None:
        self._lessons: List[Lesson] = []

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
        return [l for l in self._lessons if l.is_active]

    def deactivate_lesson(self, lesson_id: str) -> bool:
        for l in self._lessons:
            if l.id == lesson_id:
                l.is_active = False
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
            l.times_cited += 1
            lines.append(f"{idx}. [{l.category}] {l.lesson_text} (Cited {l.times_cited}x)")
        lines.append("=========================================")
        return "\n".join(lines)
