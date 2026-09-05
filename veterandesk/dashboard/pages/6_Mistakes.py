"""Mistake Detection & Independent Audit Page - Connected to Live Supabase PostgreSQL."""

from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from veterandesk.database.session import db_manager

st.set_page_config(page_title="Mistakes & Audit | VeteranDesk", page_icon="🚨", layout="wide")
st.title("🚨 Independent Mistake Detection & Audit Log")

st.markdown("""
> **Audit Decoupling:** Runs completely independent of the Risk Engine to detect any bypasses, oversizing, or timing breaches.
""")

client = db_manager.get_client()

# Fetch live audit logs from Supabase
mistakes_data: List[Dict[str, Any]] = []
try:
    res = client.table("mistake_audit_log").select("*").order("id", desc=True).execute()
    mistakes_data = res.data or []
except Exception as ex:
    st.error(f"Failed to fetch audit log from Supabase: {ex}")

violations_count = len(mistakes_data)
discrepancies_count = len([m for m in mistakes_data if m.get("risk_engine_verdict") != m.get("audit_verdict")])

col1, col2 = st.columns(2)
col1.metric(
    "Recorded Audit Discrepancies",
    f"{violations_count}",
    delta="Clean" if violations_count == 0 else f"{violations_count} Discrepancies",
    delta_color="normal" if violations_count == 0 else "inverse"
)
col2.metric(
    "Risk Engine vs Audit Discrepancies",
    f"{discrepancies_count}",
    delta="No Discrepancies" if discrepancies_count == 0 else "Bypasses Flagged",
    delta_color="normal" if discrepancies_count == 0 else "inverse"
)

st.subheader("Independent Audit Log (Live from Supabase)")

if mistakes_data:
    st.warning(f"⚠️ {violations_count} audit event(s) recorded in Supabase.")
    table_data = [
        {
            "ID": m.get("id"),
            "Trade ID": m.get("trade_id"),
            "Rule Violated": m.get("rule_violated"),
            "Discrepancy Details": m.get("discrepancy_details"),
            "Risk Engine Verdict": m.get("risk_engine_verdict"),
            "Audit Verdict": m.get("audit_verdict"),
            "Audited At": str(m.get("audited_at") or "")[:19].replace("T", " "),
        }
        for m in mistakes_data
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True)
else:
    st.success("✅ Audit log clean in Supabase. Zero rule bypasses or execution discrepancies detected.")

st.info("""
**Monitored Audit Rules:**
1. Trade entered without mandatory stop-loss
2. Per-trade risk exceeded (>1.00%)
3. Entry past 15:00 PKT cutoff
4. Daily trade count exceeded (>3 trades)
5. Position held through stop loss
""")

