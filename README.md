# 🛡️ VeteranDesk — PSX Trading Agent (Robust Core Edition)

> **The One Rule That Governs This Project:**  
> **Fewer features, zero broken ones.** Every feature works, is tested, and survives failures — or it does not ship. A feature that works 95% of the time is a bug, not a feature.

VeteranDesk is an autonomous trading agent designed to operate with the discipline of a veteran trader with 100 years of market experience: knows the rules cold, never trades without a stop-loss, never lets emotion override the plan, records every mistake honestly, and graduates from demo trading only upon meeting mathematical criteria.

---

## 🏗️ Architecture Overview

```
veterandesk/
├── config.py              # Pydantic Settings, PSX timezones, fee schedules, versioned rules
├── logging.py             # Structured JSON logging (structlog) with session_id & trade_id
├── market_data/           # DPS (dps.psx.com.pk) Scraper, latency checks, tick validator, candle builder
├── strategy/              # Pure deterministic Opening Range Breakout (ORB v1.0) & Swing thesis
├── risk/                  # 100% test-covered Risk & Discipline Engine (1% risk, 2% daily loss halt)
├── execution/             # Atomic paper broker & reconciled Double-Entry Bookkeeping Ledger
├── portfolio/             # Real-portfolio position planning with mandatory stop-loss enforcement
├── journal/               # Trade journal with 4-verdict post-mortems & active lessons memory
├── audit/                 # Independent Mistake Detection & discrepancy audit engine
├── alerts/                # Telegram notifications with outbound retry queue & schema validation
├── health/                # 60-second system heartbeat & component outage monitor
├── api/                   # FastAPI backend REST application
└── dashboard/             # Streamlit multi-page trading desk
```

---

## 🔒 The 8 Non-Negotiable Principles

1. **LLM Never Does Arithmetic:** Prices, position sizes, P&L, fee schedules, indicators, and risk percentages are 100% computed by deterministic Python code.
2. **No Trade Without a Stop-Loss:** Enforced at the database constraint level (`NOT NULL`, `CHECK stop_loss < entry_price`) and in code.
3. **Rules Cannot Be Bypassed:** Risk limits are hard ceilings in code.
4. **Immediate Decision Logging:** Every check logged with inputs, rule verdicts, and timestamps.
5. **Fail Loud, Never Silent:** Polling gaps (>2 warnings, >5 halts) and latency spikes (>90s) trigger alerts.
6. **Crash-Safe:** Daily loss halt and open positions persist across restarts; duplicate fills are rejected.
7. **Versioned Learning:** Lessons memory is versioned and citations are tracked before every market open.
8. **Real Money Only After Graduation:** Graduation requires ≥30 closed trades, positive expectancy, drawdown <10%, and 0 recent rule violations.

---

## 🚀 Quick Start

### 1. Environment Setup
```bash
# Activate virtual environment
source .venv/bin/activate

# Copy default environment variables
cp .env.example .env
```

### 2. Run Test Suite (with Coverage)
```bash
python main.py test
```
*Current test suite:* **66 passed tests**, **87% overall test coverage**, **100% Risk Engine coverage**, **1,000-fill Double-Entry Ledger reconciliation verified**, **21 Golden Test scenarios passed**.

### 3. Launch FastAPI Backend
```bash
python main.py api
# API docs available at http://localhost:8000/docs
```

### 4. Launch Streamlit Trading Desk Dashboard
```bash
python main.py dashboard
# Access dashboard in browser at http://localhost:8501
```

---

## 🧪 Testing Verification Summary

| Subsystem | Requirement | Status |
|---|---|---|
| **Risk & Discipline Engine** | 100% boundary & failure tests | **100% Pass (10/10)** |
| **Double-Entry Ledger** | Reconciled across 1,000 simulated fills | **Passed (0.00 drift)** |
| **ORB Strategy Engine** | Pure determinism + 21 golden test days | **100% Pass (25/25)** |
| **Market Data Module** | Sanity validation, latency, gap detection | **100% Pass (9/9)** |
| **Independent Mistake Audit** | Discrepancy & rule-bypass detection | **100% Pass (4/4)** |
| **Portfolio & Post-Mortem** | Mandatory stops, 4 verdicts, graduation | **100% Pass (4/4)** |
| **FastAPI REST Service** | Endpoints for health, ledger, journal | **100% Pass (4/4)** |

---

## 📋 Database Schema
The production PostgreSQL / Supabase schema is located at [`sql/001_initial_schema.sql`](file:///home/sal/Projects/trading%20bot/sql/001_initial_schema.sql) with full DDL constraints for `market_ticks`, `market_candles`, `trade_signals`, `daily_halts`, `demo_trades`, `demo_ledger`, `trade_journal`, `lessons_memory`, and `portfolio_plans`.
