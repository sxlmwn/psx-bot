"""Trade Journal & Claude Post-Mortems Page."""

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Journal & Post-Mortems | VeteranDesk", page_icon="📖", layout="wide")
st.title("📖 Trade Journal & 4-Verdict Post-Mortems")

st.markdown("""
Every closed trade receives a structured post-mortem with one of four explicit verdicts:
- **Right:** Followed plan, positive outcome.
- **Wrong:** Discipline or execution failure.
- **Right-for-wrong-reason:** Lucky profit despite rule violation (counted as error).
- **Wrong-for-right-reason:** Disciplined loss where risk was properly managed.
""")

sample_journal = [
    {
        "Trade ID": "TRD_OGDC_171000",
        "Ticker": "OGDC",
        "Verdict": "Right",
        "Net PnL (PKR)": "+6,420.00",
        "Post-Mortem Analysis": "ORB breakout triggered with 2.1x volume surge. Target reached at 146.50 without breaking trailing stop.",
        "Transferable Lesson": "Momentum breakouts with >2x volume expansion carry high reliability on energy tickers.",
        "Status": "COMPLETED"
    },
    {
        "Trade ID": "TRD_LUCK_171050",
        "Ticker": "LUCK",
        "Verdict": "Wrong-for-right-reason",
        "Net PnL (PKR)": "-3,150.00",
        "Post-Mortem Analysis": "Breakout failed at 10:15 PKT. Stop loss hit and immediately exited. Risk remained within 0.8% cap.",
        "Transferable Lesson": "Accepting disciplined stop losses prevents large drawdowns during choppy market regimes.",
        "Status": "COMPLETED"
    }
]

st.dataframe(pd.DataFrame(sample_journal), use_container_width=True)
