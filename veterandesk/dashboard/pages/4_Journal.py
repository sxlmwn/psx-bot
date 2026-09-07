import sys
from datetime import date, datetime
from pathlib import Path

# Ensure project root is on sys.path
_project_root = Path(__file__).resolve().parent
while _project_root.parent != _project_root and not (_project_root / "veterandesk").is_dir():
    _project_root = _project_root.parent
if (_project_root / "veterandesk").is_dir() and str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from veterandesk.dashboard.export import (
    TRADE_EXPORT_COLUMNS,
    build_trade_log_dataframe,
    fetch_export_raw_data,
    generate_csv_export,
    generate_excel_export,
)
from veterandesk.database.session import db_manager

st.set_page_config(page_title="Journal & Post-Mortems | VeteranDesk", page_icon="📖", layout="wide")
st.title("📖 Trade Journal & 4-Verdict Post-Mortems")

st.markdown("""
Every closed trade receives a structured post-mortem with one of four explicit verdicts:
- **Right:** Followed plan, positive outcome.
- **Wrong:** Discipline or execution failure.
- **Right-for-wrong-reason:** Lucky profit despite rule violation (counted as error).
- **Wrong-for-right-reason:** Disciplined loss where risk was properly managed.
""")

client = db_manager.get_client()

# Fetch live snapshot of trades, journals, and mistakes
raw_trades, journal_records, raw_mistakes = fetch_export_raw_data(client)

# Top metrics
total_entries = len(journal_records)
right_count = len([r for r in journal_records if r.get("verdict") == "Right"])
wrong_count = len([r for r in journal_records if r.get("verdict") == "Wrong"])
right_wrong_reason = len([r for r in journal_records if r.get("verdict") == "Right-for-wrong-reason"])
wrong_right_reason = len([r for r in journal_records if r.get("verdict") == "Wrong-for-right-reason"])

c1, c2, c3, c4 = st.columns(4)
c1.metric("Right (Plan & Profit)", right_count)
c2.metric("Wrong-for-Right-Reason (Clean Loss)", wrong_right_reason)
c3.metric("Right-for-Wrong-Reason (Friction/Error)", right_wrong_reason)
c4.metric("Wrong (Discipline Failure)", wrong_count)

st.markdown("---")

# 📥 EXPORT LOG SHEET SECTION
st.subheader("📥 Export Trade Log Sheet")
st.caption("Download an audit-ready trade log with full post-mortem analysis, 4-verdict ratings, and risk compliance flags.")

with st.container():
    if not raw_trades:
        st.info("ℹ️ No trades available to export yet. Trades will appear once the bot executes orders during market hours.")
        dl_col1, dl_col2 = st.columns(2)
        dl_col1.button("📥 Download Excel Log Sheet (.xlsx)", disabled=True, use_container_width=True, help="No trade data available yet")
        dl_col2.button("📄 Download CSV Log Sheet (.csv)", disabled=True, use_container_width=True, help="No trade data available yet")
    else:
        # Determine date bounds from existing trades
        trade_dates: List[date] = []
        for t in raw_trades:
            d_str = str(t.get("opened_at") or "")[:10]
            if d_str:
                try:
                    trade_dates.append(datetime.strptime(d_str, "%Y-%m-%d").date())
                except ValueError:
                    pass

        min_date = min(trade_dates) if trade_dates else date.today()
        max_date = max(trade_dates) if trade_dates else date.today()

        # Available unique tickers
        tickers_list = sorted(list(set(str(t.get("ticker", "")).strip().upper() for t in raw_trades if t.get("ticker"))))

        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        from_date = f_col1.date_input("From Date", value=min_date)
        to_date = f_col2.date_input("To Date", value=max_date)
        selected_ticker = f_col3.selectbox("Filter by Ticker", options=["All"] + tickers_list)
        selected_verdict = f_col4.selectbox(
            "Filter by Verdict",
            options=["All", "Right", "Wrong", "Right-for-wrong-reason", "Wrong-for-right-reason", "Pending", "Open"],
        )

        if from_date > to_date:
            st.warning("⚠️ 'From Date' cannot be later than 'To Date'.")
            export_df = pd.DataFrame(columns=TRADE_EXPORT_COLUMNS)
        else:
            export_df = build_trade_log_dataframe(
                trades=raw_trades,
                journals=journal_records,
                mistakes=raw_mistakes,
                start_date=from_date,
                end_date=to_date,
                ticker_filter=selected_ticker,
                verdict_filter=selected_verdict,
            )

        if export_df.empty:
            st.warning("⚠️ No trades match the selected filter criteria. Try expanding your date range or clearing filters.")
        else:
            st.markdown(f"**Filtered Trades ({len(export_df)} matching of {len(raw_trades)} total):**")
            st.dataframe(export_df.head(10), use_container_width=True)
            if len(export_df) > 10:
                st.caption(f"Previewing first 10 of {len(export_df)} records. Full dataset will be included in download.")

            # In-memory file generation
            today_stamp = datetime.now().strftime("%Y-%m-%d")
            excel_filename = f"veterandesk_journal_export_{today_stamp}.xlsx"
            csv_filename = f"veterandesk_journal_export_{today_stamp}.csv"

            excel_data = generate_excel_export(export_df, sheet_name="Trade_Log_Sheet")
            csv_data = generate_csv_export(export_df)

            btn_col1, btn_col2 = st.columns(2)
            btn_col1.download_button(
                label="📥 Download Excel Log Sheet (.xlsx)",
                data=excel_data,
                file_name=excel_filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            btn_col2.download_button(
                label="📄 Download CSV Log Sheet (.csv)",
                data=csv_data,
                file_name=csv_filename,
                mime="text/csv",
                use_container_width=True,
            )

st.markdown("---")
st.subheader(f"Completed Post-Mortems ({total_entries} Records in Supabase)")

if journal_records:
    table_data = [
        {
            "Trade ID": r.get("trade_id"),
            "Verdict": r.get("verdict"),
            "Status": r.get("post_mortem_status"),
            "Entry Rationale": r.get("entry_rationale"),
            "Exit Rationale": r.get("exit_rationale"),
            "Post-Mortem Analysis": r.get("post_mortem_analysis"),
            "Transferable Lesson": r.get("transferable_lesson"),
            "Created": str(r.get("created_at") or "")[:19].replace("T", " "),
        }
        for r in journal_records
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True)
else:
    st.info("No journal entries found in Supabase. Closed trades will automatically generate post-mortems here.")
