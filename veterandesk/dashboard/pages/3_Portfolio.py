"""Real Portfolio Position Plans Page."""

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Portfolio Plans | VeteranDesk", page_icon="📊", layout="wide")
st.title("📊 Real Portfolio Position Plans")

st.markdown("> **Strict Rule:** No position can be saved or tracked without a mandatory stop-loss.")

with st.expander("➕ Add / Plan New Real Position", expanded=False):
    with st.form("new_position_form"):
        ticker = st.text_input("Ticker Symbol", value="ENGRO").upper()
        qty = st.number_input("Shares Quantity", min_value=1, value=500)
        entry_price = st.number_input("Entry Fill Price (PKR)", min_value=1.0, value=320.0)
        stop_loss = st.number_input("Stop Loss (PKR - Mandatory)", min_value=0.1, value=305.0)
        target_price = st.number_input("Target Price (PKR)", min_value=1.0, value=350.0)
        submitted = st.form_submit_button("Generate Immutable Position Plan")
        if submitted:
            if stop_loss >= entry_price:
                st.error("Stop loss must be strictly below entry price!")
            else:
                st.success(f"Position plan generated for {ticker}. Plan is immutable.")

st.subheader("Active Position Plans & Session Calls")
plans = [
    {
        "Ticker": "ENGRO",
        "Quantity": 500,
        "Entry (PKR)": 320.0,
        "Stop Loss (PKR)": 305.0,
        "Target (PKR)": 350.0,
        "Trim Level (PKR)": 335.0,
        "Account Risk %": "0.75%",
        "Session Call": "HOLD (Within plan)",
        "Oversize Warning": "No"
    }
]
st.dataframe(pd.DataFrame(plans), use_container_width=True)
