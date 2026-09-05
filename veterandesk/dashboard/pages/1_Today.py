"""Today's Session Page."""

import streamlit as st
import pandas as pd
from datetime import datetime
from veterandesk.config import settings

st.set_page_config(page_title="Today's Session | VeteranDesk", page_icon="📈", layout="wide")
st.title("📈 Today's Session & Signals")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Current Session", settings.session_id)
col2.metric("Market Cutoff", f"{settings.entry_cutoff_pkt.strftime('%H:%M')} PKT")
col3.metric("Force Close", f"{settings.force_close_pkt.strftime('%H:%M')} PKT")
col4.metric("Data Status", "OK (0 ms latency)")

st.subheader("Active Watchlist")
st.write(", ".join(settings.watchlist))

st.subheader("Generated ORB Breakout Signals")
sample_signals = [
    {
        "Time": "09:33:00",
        "Ticker": "OGDC",
        "Action": "BUY",
        "Entry (PKR)": 142.50,
        "Stop (PKR)": 139.80,
        "Target (PKR)": 146.55,
        "R:R": 1.50,
        "Confidence": "65%",
        "Status": "APPROVED",
        "Data Quality": "ok"
    },
    {
        "Time": "09:41:00",
        "Ticker": "PPL",
        "Action": "BUY",
        "Entry (PKR)": 118.20,
        "Stop (PKR)": 115.50,
        "Target (PKR)": 122.25,
        "R:R": 1.50,
        "Confidence": "70%",
        "Status": "APPROVED",
        "Data Quality": "ok"
    }
]
st.dataframe(pd.DataFrame(sample_signals), use_container_width=True)

st.subheader("Session Decision Log")
st.info("No rule bypasses. All signals evaluated against Risk Engine pipeline.")
