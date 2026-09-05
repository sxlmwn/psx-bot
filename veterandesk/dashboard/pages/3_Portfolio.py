"""Real Portfolio Position Plans Page - Connected to Live Supabase PostgreSQL."""

from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from veterandesk.database.session import db_manager
from veterandesk.portfolio.manager import PortfolioManager

st.set_page_config(page_title="Portfolio Plans | VeteranDesk", page_icon="📊", layout="wide")
st.title("📊 Real Portfolio Position Plans")

st.markdown("> **Strict Rule:** No position can be saved or tracked without a mandatory stop-loss.")

client = db_manager.get_client()

with st.expander("➕ Add / Plan New Real Position", expanded=False):
    with st.form("new_position_form"):
        ticker = st.text_input("Ticker Symbol", value="ENGRO").upper().strip()
        qty = int(st.number_input("Shares Quantity", min_value=1, value=500))
        entry_price = float(st.number_input("Entry Fill Price (PKR)", min_value=1.0, value=320.0))
        stop_loss = float(st.number_input("Stop Loss (PKR - Mandatory)", min_value=0.1, value=305.0))
        target_price = float(st.number_input("Target Price (PKR)", min_value=1.0, value=350.0))
        submitted = st.form_submit_button("Generate Immutable Position Plan")
        if submitted:
            if stop_loss >= entry_price:
                st.error("Stop loss must be strictly below entry price!")
            elif target_price <= entry_price:
                st.error("Target price must be strictly above entry price!")
            else:
                mgr = PortfolioManager()
                try:
                    plan = mgr.create_position_plan(
                        ticker=ticker,
                        quantity=qty,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        target_price=target_price,
                    )
                    row = {
                        "ticker": plan.ticker,
                        "quantity": plan.quantity,
                        "entry_fill_price": plan.entry_fill_price,
                        "stop_loss": plan.stop_loss,
                        "target_price": plan.target_price,
                        "trim_level": plan.trim_level,
                        "max_risk_pct": plan.max_risk_pct,
                        "oversize_warning": plan.oversize_warning,
                        "shares_to_trim": plan.shares_to_trim,
                        "plan_version": plan.plan_version,
                        "status": "ACTIVE",
                    }
                    client.table("portfolio_plans").insert(row).execute()
                    st.success(f"Position plan for {ticker} generated and persisted to Supabase.")
                except Exception as ex:
                    st.error(f"Failed to create position plan: {ex}")

# Fetch live plans from Supabase
live_plans: List[Dict[str, Any]] = []
try:
    res = client.table("portfolio_plans").select("*").order("id", desc=True).execute()
    live_plans = res.data or []
except Exception as ex:
    st.error(f"Failed to fetch portfolio plans: {ex}")

st.subheader("Active Position Plans (Live from Supabase)")

if live_plans:
    col1, col2, col3 = st.columns(3)
    col1.metric("Active Plans", len(live_plans))
    total_alloc = sum([float(p.get("quantity", 0)) * float(p.get("entry_fill_price", 0)) for p in live_plans], 0.0)
    col2.metric("Total Planned Capital", f"PKR {total_alloc:,.2f}")
    oversized = len([p for p in live_plans if p.get("oversize_warning")])
    col3.metric("Oversized Warnings", oversized)

    table_data = [
        {
            "ID": p.get("id"),
            "Ticker": p.get("ticker"),
            "Quantity": p.get("quantity"),
            "Entry (PKR)": f"{float(p.get('entry_fill_price', 0)):,.2f}",
            "Stop Loss (PKR)": f"{float(p.get('stop_loss', 0)):,.2f}",
            "Target (PKR)": f"{float(p.get('target_price', 0)):,.2f}",
            "Trim Level (PKR)": f"{float(p.get('trim_level', 0)):,.2f}" if p.get("trim_level") else "N/A",
            "Risk %": f"{float(p.get('max_risk_pct', 0)):.2f}%",
            "Oversized": "⚠️ YES" if p.get("oversize_warning") else "No",
            "Status": p.get("status"),
        }
        for p in live_plans
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True)
else:
    st.info("No position plans found in Supabase. Use the form above to add an immutable plan.")
