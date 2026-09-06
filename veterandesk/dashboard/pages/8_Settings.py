"""Settings and Configuration Page - Connected to Live Supabase PostgreSQL."""

from typing import Any, Dict, List
import streamlit as st
import json
from veterandesk.config import settings, fee_structure
from veterandesk.database.session import db_manager

st.set_page_config(page_title="Settings | VeteranDesk", page_icon="⚙️", layout="wide")
st.title("⚙️ System Configuration & Fee Schedule")

st.markdown("> **Safety Note:** Risk limits are strictly capped and can only be tuned downward in code.")

client = db_manager.get_client()

# Fetch live rules from Supabase
db_rules: List[Dict[str, Any]] = []
try:
    res = client.table("rules_config").select("*").order("id", desc=True).limit(1).execute()
    db_rules = res.data or []
except Exception as ex:
    st.warning(f"Could not load rules_config from Supabase: {ex}")

tab1, tab2, tab3, tab4 = st.tabs(["Risk Thresholds (Supabase)", "PSX Fee Table", "Watchlist", "Telegram Alerts"])

with tab1:
    st.subheader("Hard Risk Parameters (Enforced in Database & Code)")
    if db_rules:
        r = db_rules[0]
        st.success(f"Loaded active config from Supabase rules_config (Version: {r.get('version', 'N/A')})")
        c1, c2, c3 = st.columns(3)
        c1.metric("Max Risk / Trade", f"{float(r.get('max_risk_pct_per_trade', 1.0)):.2f}%", help="Hard cap: 1.00%")
        c2.metric("Daily Loss Halt", f"{float(r.get('max_daily_loss_pct', 2.0)):.2f}%", help="Triggers daily trading halt at 2.00%")
        c3.metric("Max Intraday Trades", f"{int(r.get('max_intraday_trades_per_day', 3))}")

        c4, c5, c6 = st.columns(3)
        c4.metric("Entry Cutoff (PKT)", f"{r.get('entry_cutoff_time_pkt', '15:00:00')}")
        c5.metric("Force Close (PKT)", f"{r.get('force_close_time_pkt', '15:20:00')}")
        c6.metric("Max 20-day ADV %", f"{float(r.get('max_adv_pct', 5.0)):.2f}%")
    else:
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

with tab4:
    st.subheader("📲 Telegram Bot Alerts & Outbound Queue")
    from veterandesk.alerts.telegram import get_delivery_stats
    stats = get_delivery_stats()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Dispatched", stats["total"])
    m2.metric("Delivered (Sent)", stats["sent"])
    m3.metric("Pending In-Flight", stats["pending"])
    skipped_count = stats.get("skipped", 0)
    m4.metric(
        "Skipped / Mock",
        skipped_count,
        delta="⚠️ Unconfigured" if skipped_count > 0 else "0 Skipped",
        delta_color="inverse" if skipped_count > 0 else "normal",
        help="Alerts skipped because Telegram credentials are missing/disabled",
    )
    m5.metric("Failed (Retries Exhausted)", stats["failed"], delta="Clean" if stats["failed"] == 0 else "Needs Review", delta_color="normal" if stats["failed"] == 0 else "inverse")

    token_configured = bool(settings.telegram_bot_token and str(settings.telegram_bot_token).strip())
    chat_configured = bool(settings.telegram_chat_id and str(settings.telegram_chat_id).strip())
    is_live = bool(settings.telegram_enabled and token_configured and chat_configured)

    if is_live:
        st.success("🟢 **Telegram Delivery Active**: Bot token and Chat ID are configured. Live alerts will be transmitted.")
    else:
        st.warning(
            "⚠️ **Telegram Delivery Disabled / Offline**: Alerts are logged with status `skipped` instead of being sent. "
            "To enable live delivery, configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`."
        )

    st.markdown("### Delivery Engine Guarantees")
    st.markdown(
        "- **Bot Target:** `@Veterandesk_bot`\n"
        "- **Retry Policy:** Up to 3 attempts with exponential backoff (`0.5s`, `1.0s`, `2.0s`).\n"
        "- **Rate Limit:** 1.0s minimum inter-message delay.\n"
        "- **Persistence:** Every outbound alert logged to `telegram_delivery_log` (Supabase PostgreSQL / SQLite fallback).\n"
        "- **Discipline Schedule:** Daily Brief at `9:15 AM PKT`, Session Summary at `3:45 PM PKT`."
    )


