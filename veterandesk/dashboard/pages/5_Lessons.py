"""Lessons Memory Page."""

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Lessons Memory | VeteranDesk", page_icon="🧠", layout="wide")
st.title("🧠 Lessons Memory")

st.markdown("> **Pre-Session Context Injection:** All active lessons are automatically loaded before market open.")

lessons = [
    {
        "ID": "LES_001",
        "Category": "ORB_OGDC",
        "Lesson": "Momentum breakouts with >2x volume expansion carry high reliability on energy tickers.",
        "Times Cited": 14,
        "Active": True
    },
    {
        "ID": "LES_002",
        "Category": "RISK_DISCIPLINE",
        "Lesson": "Taking a planned stop loss protects capital and proves disciplined execution; losses are regular business costs.",
        "Times Cited": 22,
        "Active": True
    },
    {
        "ID": "LES_003",
        "Category": "SESSION_TIMING",
        "Lesson": "Intraday discipline requires exiting before 15:20 PKT regardless of emotion or expectation.",
        "Times Cited": 18,
        "Active": True
    }
]

st.dataframe(pd.DataFrame(lessons), use_container_width=True)

st.subheader("Pre-Session Prompt Injection Preview")
st.code("""=== VETERANDESK ACTIVE LESSONS MEMORY ===
1. [ORB_OGDC] Momentum breakouts with >2x volume expansion carry high reliability on energy tickers.
2. [RISK_DISCIPLINE] Taking a planned stop loss protects capital; losses are regular business costs.
3. [SESSION_TIMING] Intraday discipline requires exiting before 15:20 PKT regardless of emotion.
=========================================""", language="text")
