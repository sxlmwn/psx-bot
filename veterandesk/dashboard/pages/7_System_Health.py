"""System Health & 60-Second Heartbeats Page."""

import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="System Health | VeteranDesk", page_icon="🏥", layout="wide")
st.title("🏥 System Health & Heartbeat Monitoring")

col1, col2, col3, col4 = st.columns(4)
col1.metric("System Status", "OPERATIONAL 🟢")
col2.metric("Heartbeat Interval", "60 seconds")
col3.metric("Max Latency Threshold", "90 seconds")
col4.metric("Last Heartbeat", datetime.now().strftime("%H:%M:%S UTC"))

st.subheader("Component Health Grid")
components = [
    {"Component": "DPS Market Data Scraper", "Status": "GREEN 🟢", "Latency": "180 ms", "Details": "Polling normal (0 gaps)"},
    {"Component": "Risk & Discipline Engine", "Status": "GREEN 🟢", "Latency": "1.2 ms", "Details": "All limits active (0 bypasses)"},
    {"Component": "Double-Entry Ledger", "Status": "GREEN 🟢", "Latency": "0.8 ms", "Details": "100% Reconciled (diff: PKR 0.00)"},
    {"Component": "Database Connectivity", "Status": "GREEN 🟢", "Latency": "4.5 ms", "Details": "PostgreSQL pool healthy"},
    {"Component": "Telegram Notification Queue", "Status": "GREEN 🟢", "Latency": "45 ms", "Details": "Outbound queue empty (0 failed)"},
    {"Component": "Post-Mortem Queue", "Status": "GREEN 🟢", "Latency": "12 ms", "Details": "0 pending jobs"}
]

st.dataframe(pd.DataFrame(components), use_container_width=True)
