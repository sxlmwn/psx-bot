"""Lessons Memory Page - Connected to Live Supabase PostgreSQL."""

from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from veterandesk.database.session import db_manager
from veterandesk.journal.lessons import LessonsMemory

st.set_page_config(page_title="Lessons Memory | VeteranDesk", page_icon="🧠", layout="wide")
st.title("🧠 Lessons Memory")

st.markdown("> **Pre-Session Context Injection:** All active lessons are automatically loaded from Supabase before market open.")

client = db_manager.get_client()

# Fetch live lessons from Supabase
lessons_data: List[Dict[str, Any]] = []
try:
    res = client.table("lessons_memory").select("*").order("id", desc=False).execute()
    lessons_data = res.data or []
except Exception as ex:
    st.error(f"Failed to fetch lessons from Supabase: {ex}")

if lessons_data:
    col1, col2, col3 = st.columns(3)
    active_count = len([l for l in lessons_data if l.get("is_active")])
    col1.metric("Total Lessons", len(lessons_data))
    col2.metric("Active Lessons", active_count)
    total_citations = sum([int(l.get("times_cited", 0)) for l in lessons_data], 0)
    col3.metric("Total Times Cited", total_citations)

    st.markdown("---")
    st.subheader("Active Transferable Lessons (Live from Supabase)")

    table_data = [
        {
            "ID": l.get("id"),
            "Category": l.get("category"),
            "Lesson": l.get("lesson_text"),
            "Times Cited": l.get("times_cited"),
            "Active": "✅ Yes" if l.get("is_active") else "❌ No",
            "Created": str(l.get("created_at") or "")[:19].replace("T", " "),
        }
        for l in lessons_data
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True)

    st.subheader("Live Pre-Session Prompt Injection Preview")
    mem = LessonsMemory(sync_with_db=False)
    for l in lessons_data:
        if l.get("is_active"):
            mem.add_lesson(
                category=l.get("category", "GENERAL"),
                text=l.get("lesson_text", ""),
                trade_id=l.get("trade_id"),
            )
    prompt_preview = mem.build_pre_session_prompt_context()
    st.code(prompt_preview, language="text")
else:
    st.info("No lessons found in Supabase. Completed trade post-mortems will extract and store transferable lessons here.")

