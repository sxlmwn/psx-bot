"""
Streamlit Main Dashboard Entry: VeteranDesk PSX Trading Desk.
"""

import streamlit as st

st.set_page_config(
    page_title="VeteranDesk | PSX Trading Desk",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🛡️ VeteranDesk — PSX Autonomous Trading Desk")
st.caption("Disciplined Quantitative Trading • PSX Equities • Demo Graduation Engine")

st.markdown("""
> **The Golden Rule:** *Fewer features, zero broken ones. Trust is the product.*

### Core System Invariants
- **Deterministic Arithmetic:** Zero LLM price or size calculations.
- **Mandatory Stops:** Enforced in code and database constraints.
- **Strict Risk Cap:** Max 1% risk per trade, 2% daily loss halt, max 3 intraday trades.
- **Double-Entry Ledger:** Invariant reconciliation holds across every transaction.
- **Graduation Standard:** 30+ trades, positive expectancy, drawdown < 10%, clean recent history.

---
### System Navigation
Use the sidebar on the left to navigate the desk modules:
1. **Today:** Real-time PSX feed, ORB breakout signals, session status.
2. **Demo Account:** Account balance, double-entry ledger, equity curve, graduation status.
3. **Portfolio:** Real holdings, immutable position plans, hold/trim/exit calls.
4. **Journal:** Closed trades, Groq (openai/gpt-oss-120b) 4-verdict post-mortems.
5. **Lessons:** Active memory of disciplined rules cited before each session.
6. **Mistakes:** Independent post-trade audit log & discrepancy alerts.
7. **System Health:** 60s heartbeats, component latencies, failure tracking.
8. **Settings:** Risk thresholds, fee table, and watchlist.
""")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Strategy", "ORB v1.0 (15-min)")
col2.metric("Market", "PSX (KSE-100)")
col3.metric("Max Trade Risk", "1.00%")
col4.metric("Daily Loss Halt", "2.00%")

st.info("System operational. Select a page from the left sidebar.")
