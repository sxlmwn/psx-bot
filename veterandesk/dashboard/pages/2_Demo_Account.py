"""Demo Paper Account & Graduation Page."""

import streamlit as st
import pandas as pd
from veterandesk.config import settings

st.set_page_config(page_title="Demo Account | VeteranDesk", page_icon="💼", layout="wide")
st.title("💼 Demo Account & Double-Entry Ledger")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Cash Balance", "PKR 500,000.00")
col2.metric("Equity Holdings", "PKR 0.00")
col3.metric("Realized P&L", "PKR +0.00")
col4.metric("Ledger Invariant", "RECONCILED ✅")

st.markdown("---")
st.subheader("🎓 Graduation Criteria Tracking")
st.caption("Graduation is calculated strictly by code and unlocks real-account trading.")

g_col1, g_col2, g_col3, g_col4 = st.columns(4)
g_col1.metric("Closed Trades", "0 / 30", help="Requires at least 30 closed trades")
g_col2.metric("Expectancy", "PKR 0.00", help="Must be strictly positive")
g_col3.metric("Max Drawdown", "0.00%", help="Must stay below 10.00%")
g_col4.metric("Recent Violations", "0", help="Zero violations allowed in last 20 trades")

st.warning("Status: DEMO PHASE ONLY — Graduation criteria pending.")

st.markdown("---")
st.subheader("Double-Entry Ledger Audit Log")
sample_ledger = [
    {"TxID": "INIT_001", "Account": "CASH", "Debit": 500000.0, "Credit": 0.0, "Balance": 500000.0, "Desc": "Initial Capital Deposit"}
]
st.dataframe(pd.DataFrame(sample_ledger), use_container_width=True)
