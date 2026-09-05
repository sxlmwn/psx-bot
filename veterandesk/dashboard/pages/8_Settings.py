"""Settings and Configuration Page."""

import streamlit as st
import json
from veterandesk.config import settings, fee_structure

st.set_page_config(page_title="Settings | VeteranDesk", page_icon="⚙️", layout="wide")
st.title("⚙️ System Configuration & Fee Schedule")

st.markdown("> **Safety Note:** Risk limits are strictly capped and can only be tuned downward in code.")

tab1, tab2, tab3 = st.tabs(["Risk Thresholds", "PSX Fee Table", "Watchlist"])

with tab1:
    st.subheader("Hard Risk Parameters")
    st.number_input("Max Risk Per Trade (%)", value=settings.max_risk_per_trade_pct, disabled=True, help="Hard-capped at 1.00%")
    st.number_input("Max Daily Loss (%)", value=settings.max_daily_loss_pct, disabled=True, help="Triggers daily trading halt at 2.00%")
    st.number_input("Max Intraday Trades Per Day", value=settings.max_intraday_trades_per_day, disabled=True)
    st.time_input("Entry Cutoff Time (PKT)", value=settings.entry_cutoff_pkt, disabled=True)
    st.time_input("Force Close Time (PKT)", value=settings.force_close_pkt, disabled=True)

with tab2:
    st.subheader(f"Versioned Fee Structure: {fee_structure.version}")
    fees = {
        "Broker Commission": f"{fee_structure.broker_commission_pct * 100:.2f}%",
        "SECP Turnover Levy": f"{fee_structure.secp_turnover_pct * 100:.4f}%",
        "NCCPL Charges": f"{fee_structure.nccpl_charges_pct * 100:.4f}%",
        "CGT Withholding": f"{fee_structure.cgt_withholding_pct * 100:.1f}% on net gains",
        "Default Slippage Model": f"{fee_structure.default_slippage_pct * 100:.2f}% (0.10% - 0.30%)"
    }
    st.json(fees)

with tab3:
    st.subheader("Liquid Watchlist")
    st.write(settings.watchlist)
