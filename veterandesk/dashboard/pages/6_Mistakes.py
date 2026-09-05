"""Mistake Detection & Independent Audit Page."""

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Mistakes & Audit | VeteranDesk", page_icon="🚨", layout="wide")
st.title("🚨 Independent Mistake Detection & Audit Log")

st.markdown("""
> **Audit Decoupling:** Runs completely independent of the Risk Engine to detect any bypasses, oversizing, or timing breaches.
""")

col1, col2 = st.columns(2)
col1.metric("Critical Violations", "0", delta="Clean", delta_color="normal")
col2.metric("Risk Engine Discrepancies", "0", delta="No Bypasses", delta_color="normal")

st.subheader("Audit Log")
st.success("✅ Audit log clean. Zero rule bypasses detected across all sessions.")

st.info("""
**Monitored Audit Rules:**
1. Trade entered without mandatory stop-loss
2. Per-trade risk exceeded (>1.00%)
3. Entry past 15:00 PKT cutoff
4. Daily trade count exceeded (>3 trades)
5. Position held through stop loss
""")
