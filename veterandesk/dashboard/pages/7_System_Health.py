"""System Health & 60-Second Heartbeats Page - Connected to Live Supabase PostgreSQL."""

from datetime import datetime, timezone
import pandas as pd
import streamlit as st

from veterandesk.database.session import db_manager

st.set_page_config(page_title="System Health | VeteranDesk", page_icon="🏥", layout="wide")
st.title("🏥 System Health & Heartbeat Monitoring")

client = db_manager.get_client()

# 1. Real-time ping to Supabase PostgreSQL
db_check = db_manager.check_connection()
is_green = db_check["status"] == "GREEN"

col1, col2, col3, col4 = st.columns(4)
col1.metric("Overall System Status", "OPERATIONAL 🟢" if is_green else "DEGRADED 🔴")
col2.metric("Heartbeat Frequency", "60 seconds")
col3.metric("Max Latency Cap", "90 seconds")
col4.metric("Live Supabase Latency", f"{db_check.get('latency_ms', 0):.1f} ms")

st.markdown("---")
st.subheader("Supabase PostgreSQL Health Status")
if is_green:
    st.success(f"✅ {db_check['message']} (Provider: {db_check.get('provider', 'Supabase')})")
else:
    st.error(f"❌ Database error: {db_check.get('message', 'Failed to connect')}")

st.markdown("---")
st.subheader("Live Component Heartbeat Grid (Supabase: `health_heartbeats`)")

try:
    res = client.table("health_heartbeats").select("*").order("checked_at", desc=True).limit(20).execute()
    heartbeats = res.data or []
    if heartbeats:
        df_hb = pd.DataFrame(heartbeats)
        display_cols = ["id", "component", "status", "latency_ms", "message", "checked_at"]
        avail_cols = [c for c in display_cols if c in df_hb.columns]
        st.dataframe(df_hb[avail_cols], use_container_width=True)

        # Check silence threshold (>120s)
        latest_ts_str = heartbeats[0]["checked_at"]
        latest_dt = datetime.fromisoformat(latest_ts_str.replace("Z", "+00:00"))
        seconds_ago = (datetime.now(timezone.utc) - latest_dt).total_seconds()

        if seconds_ago > 120:
            st.error(f"🚨 SILENCE ALERT: No heartbeat received for {seconds_ago:.0f} seconds (> 120s threshold)!")
        else:
            st.info(f"⏱ Heartbeat healthy: Last checked {seconds_ago:.0f}s ago (within 120s threshold).")
    else:
        st.info("No heartbeat records found in `health_heartbeats` table.")
except Exception as e:
    st.error(f"Failed to query `health_heartbeats` from Supabase: {e}")

st.markdown("---")
st.subheader("📲 Telegram Outbound Delivery & Outbox (`telegram_delivery_log`)")

from veterandesk.alerts.telegram import get_delivery_stats

stats = get_delivery_stats()
t_col1, t_col2, t_col3, t_col4, t_col5 = st.columns(5)
t_col1.metric("Total Messages", stats["total"])
t_col2.metric("Delivered (Sent)", stats["sent"], delta=f"{stats['sent']} delivered" if stats['sent'] > 0 else None)
t_col3.metric("Pending In-Flight", stats["pending"])
skipped_val = stats.get("skipped", 0)
t_col4.metric(
    "Skipped / Mock",
    skipped_val,
    delta="⚠️ Unconfigured" if skipped_val > 0 else "0 Skipped (Clean)",
    delta_color="inverse" if skipped_val > 0 else "normal",
    help="Alerts skipped because Telegram bot token or chat ID is missing/disabled",
)
failed_val = stats["failed"]
t_col5.metric(
    "Failed (3x Retried)",
    failed_val,
    delta="🚨 Attention" if failed_val > 0 else "0 Failed (Clean)",
    delta_color="inverse" if failed_val > 0 else "normal",
    help="Messages that permanently failed after 3 exponential backoff attempts",
)

if skipped_val > 0:
    st.warning(f"⚠️ **{skipped_val} Telegram alert(s) SKIPPED**: Notifier was unconfigured or disabled. Real messages were never sent to Telegram. Check `.env` settings.")

try:
    res_tg = client.table("telegram_delivery_log").select("*").order("created_at", desc=True).limit(20).execute()
    tg_rows = res_tg.data or []
    if tg_rows:
        df_tg = pd.DataFrame(tg_rows)
        tg_display_cols = ["id", "message_type", "status", "attempts", "reference_id", "event_type", "last_error", "created_at", "sent_at", "failed_at"]
        avail_tg_cols = [c for c in tg_display_cols if c in df_tg.columns]
        st.dataframe(df_tg[avail_tg_cols], use_container_width=True)
    else:
        st.info("No outbound alerts logged yet in `telegram_delivery_log`.")
except Exception as ex_tg:
    st.warning(f"Could not load `telegram_delivery_log` from Supabase: {ex_tg}")

st.markdown("---")
st.subheader("💬 Discord Outbound Delivery & Outbox (`discord_delivery_log`)")

from veterandesk.alerts.discord import get_discord_delivery_stats

d_stats = get_discord_delivery_stats()
d_col1, d_col2, d_col3, d_col4, d_col5 = st.columns(5)
d_col1.metric("Total Messages", d_stats["total"])
d_col2.metric("Delivered (Sent)", d_stats["sent"], delta=f"{d_stats['sent']} delivered" if d_stats['sent'] > 0 else None)
d_col3.metric("Pending In-Flight", d_stats["pending"])
d_skipped_val = d_stats.get("skipped", 0)
d_col4.metric(
    "Skipped / Mock",
    d_skipped_val,
    delta="⚠️ Unconfigured" if d_skipped_val > 0 else "0 Skipped (Clean)",
    delta_color="inverse" if d_skipped_val > 0 else "normal",
    help="Alerts skipped because Discord webhook URL is missing/disabled",
)
d_failed_val = d_stats["failed"]
d_col5.metric(
    "Failed (3x Retried)",
    d_failed_val,
    delta="🚨 Attention" if d_failed_val > 0 else "0 Failed (Clean)",
    delta_color="inverse" if d_failed_val > 0 else "normal",
    help="Messages that permanently failed after 3 exponential backoff attempts",
)

if d_skipped_val > 0:
    st.warning(f"⚠️ **{d_skipped_val} Discord alert(s) SKIPPED**: Notifier was unconfigured or disabled. Real messages were never sent to Discord. Check `.env` settings.")

try:
    res_dc = client.table("discord_delivery_log").select("*").order("created_at", desc=True).limit(20).execute()
    dc_rows = res_dc.data or []
    if dc_rows:
        df_dc = pd.DataFrame(dc_rows)
        dc_display_cols = ["id", "message_type", "status", "attempts", "reference_id", "event_type", "last_error", "created_at", "sent_at", "failed_at"]
        avail_dc_cols = [c for c in dc_display_cols if c in df_dc.columns]
        st.dataframe(df_dc[avail_dc_cols], use_container_width=True)
    else:
        st.info("No outbound alerts logged yet in `discord_delivery_log`.")
except Exception as ex_dc:
    st.warning(f"Could not load `discord_delivery_log` from Supabase: {ex_dc}")


