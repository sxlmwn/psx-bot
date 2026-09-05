"""Trade Journal & 4-Verdict Post-Mortems Page - Connected to Live Supabase PostgreSQL."""

from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from veterandesk.database.session import db_manager

st.set_page_config(page_title="Journal & Post-Mortems | VeteranDesk", page_icon="📖", layout="wide")
st.title("📖 Trade Journal & 4-Verdict Post-Mortems")

st.markdown("""
Every closed trade receives a structured post-mortem with one of four explicit verdicts:
- **Right:** Followed plan, positive outcome.
- **Wrong:** Discipline or execution failure.
- **Right-for-wrong-reason:** Lucky profit despite rule violation (counted as error).
- **Wrong-for-right-reason:** Disciplined loss where risk was properly managed.
""")

client = db_manager.get_client()

# Fetch live journal from Supabase
journal_records: List[Dict[str, Any]] = []
try:
    res = client.table("trade_journal").select("*").order("id", desc=True).execute()
    journal_records = res.data or []
except Exception as ex:
    st.error(f"Failed to fetch trade journal from Supabase: {ex}")

if journal_records:
    # Top metrics
    total_entries = len(journal_records)
    right_count = len([r for r in journal_records if r.get("verdict") == "Right"])
    wrong_count = len([r for r in journal_records if r.get("verdict") == "Wrong"])
    right_wrong_reason = len([r for r in journal_records if r.get("verdict") == "Right-for-wrong-reason"])
    wrong_right_reason = len([r for r in journal_records if r.get("verdict") == "Wrong-for-right-reason"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Right (Plan & Profit)", right_count)
    c2.metric("Wrong-for-Right-Reason (Clean Loss)", wrong_right_reason)
    c3.metric("Right-for-Wrong-Reason (Friction/Error)", right_wrong_reason)
    c4.metric("Wrong (Discipline Failure)", wrong_count)

    st.markdown("---")
    st.subheader(f"Completed Post-Mortems ({total_entries} Records in Supabase)")

    table_data = [
        {
            "Trade ID": r.get("trade_id"),
            "Verdict": r.get("verdict"),
            "Status": r.get("post_mortem_status"),
            "Entry Rationale": r.get("entry_rationale"),
            "Exit Rationale": r.get("exit_rationale"),
            "Post-Mortem Analysis": r.get("post_mortem_analysis"),
            "Transferable Lesson": r.get("transferable_lesson"),
            "Created": str(r.get("created_at") or "")[:19].replace("T", " "),
        }
        for r in journal_records
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True)
else:
    st.info("No journal entries found in Supabase. Closed trades will automatically generate post-mortems here.")

