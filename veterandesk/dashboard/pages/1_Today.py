"""Today's Session Page - Connected to Live Supabase PostgreSQL."""

from datetime import datetime
import pandas as pd
import streamlit as st

from veterandesk.config import settings, PKT_TZ
from veterandesk.database.session import db_manager

st.set_page_config(page_title="Today's Session | VeteranDesk", page_icon="📈", layout="wide")
st.title("📈 Today's Session & Signals")

# Top metrics
now_pkt = datetime.now(PKT_TZ)
col1, col2, col3, col4 = st.columns(4)
col1.metric("Current Time (PKT)", now_pkt.strftime("%H:%M:%S PKT"))
col2.metric("Market Entry Cutoff", f"{settings.entry_cutoff_pkt.strftime('%H:%M')} PKT")
col3.metric("Force Close Cutoff", f"{settings.force_close_pkt.strftime('%H:%M')} PKT")

# Live Supabase DB check
db_check = db_manager.check_connection()
db_status = "ONLINE 🟢" if db_check["status"] == "GREEN" else "OFFLINE 🔴"
col4.metric("Database (Supabase)", db_status, f"{db_check.get('latency_ms', 0):.1f} ms")

st.markdown("---")
st.subheader("Active Watchlist")
st.write(", ".join([f"`{t}`" for t in settings.watchlist]))

st.markdown("---")
st.subheader("Live Signals (Supabase PostgreSQL: `trade_signals`)")

client = db_manager.get_client()
try:
    res = client.table("trade_signals").select("*").order("created_at", desc=True).limit(50).execute()
    signals_data = res.data
    if signals_data:
        df_sig = pd.DataFrame(signals_data)
        display_cols = [
            "signal_id", "ticker", "action", "entry_price", "stop_loss", 
            "target_price", "reward_risk_ratio", "position_size", "confidence_pct", "status", "created_at"
        ]
        available_cols = [c for c in display_cols if c in df_sig.columns]
        st.dataframe(df_sig[available_cols], use_container_width=True)
    else:
        st.info("No trading signals recorded yet in Supabase `trade_signals` table.")
except Exception as e:
    st.error(f"Failed to fetch signals from Supabase: {e}")

st.markdown("---")
st.subheader("Today's Executed Trades (Supabase PostgreSQL: `trades`)")
try:
    res_trades = client.table("trades").select("*").order("opened_at", desc=True).limit(50).execute()
    trades_data = res_trades.data
    if trades_data:
        df_trades = pd.DataFrame(trades_data)
        display_trades = [
            "trade_id", "ticker", "action", "shares", "entry_price", "exit_price",
            "stop_loss", "target_price", "fees_paid", "net_pnl", "status", "opened_at"
        ]
        avail_trades = [c for c in display_trades if c in df_trades.columns]
        st.dataframe(df_trades[avail_trades], use_container_width=True)
    else:
        st.info("No trades recorded yet in Supabase `trades` table.")
except Exception as e:
    st.error(f"Failed to fetch trades from Supabase: {e}")
