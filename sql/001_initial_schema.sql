-- VeteranDesk: Robust PSX Trading Agent Schema
-- Target: PostgreSQL / Supabase
-- Core Rule: Database-level enforcement of risk limits and integrity constraints.

-- 1. Versioned Rules Configuration
CREATE TABLE IF NOT EXISTS rules_config (
    id SERIAL PRIMARY KEY,
    version VARCHAR(32) NOT NULL UNIQUE,
    max_risk_pct_per_trade NUMERIC(5, 2) NOT NULL DEFAULT 1.00 CHECK (max_risk_pct_per_trade <= 1.00),
    max_daily_loss_pct NUMERIC(5, 2) NOT NULL DEFAULT 2.00 CHECK (max_daily_loss_pct <= 5.00),
    max_intraday_trades_per_day INT NOT NULL DEFAULT 3,
    entry_cutoff_time_pkt TIME NOT NULL DEFAULT '15:00:00',
    force_close_time_pkt TIME NOT NULL DEFAULT '15:20:00',
    max_adv_pct NUMERIC(5, 2) NOT NULL DEFAULT 5.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- 2. Ticks Table (Scraped Data from DPS)
-- Unique constraint enforces idempotent writes: re-scraping the same timestamp never duplicates.
CREATE TABLE IF NOT EXISTS market_ticks (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(16) NOT NULL,
    price NUMERIC(12, 4) NOT NULL CHECK (price > 0),
    volume BIGINT NOT NULL CHECK (volume >= 0),
    high NUMERIC(12, 4),
    low NUMERIC(12, 4),
    change NUMERIC(12, 4),
    change_pct NUMERIC(8, 4),
    psx_timestamp TIMESTAMPTZ NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    latency_seconds NUMERIC(8, 2) NOT NULL DEFAULT 0,
    data_status VARCHAR(16) NOT NULL DEFAULT 'ok' CHECK (data_status IN ('ok', 'degraded', 'rejected')),
    session_id VARCHAR(64) NOT NULL,
    CONSTRAINT uq_ticker_timestamp UNIQUE (ticker, psx_timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ticks_ticker_time ON market_ticks(ticker, psx_timestamp DESC);

-- 3. Candles Table (1-min, 5-min, 15-min, 1-day)
CREATE TABLE IF NOT EXISTS market_candles (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(16) NOT NULL,
    timeframe VARCHAR(8) NOT NULL CHECK (timeframe IN ('1m', '5m', '15m', '1d')),
    open_price NUMERIC(12, 4) NOT NULL CHECK (open_price > 0),
    high_price NUMERIC(12, 4) NOT NULL CHECK (high_price >= open_price),
    low_price NUMERIC(12, 4) NOT NULL CHECK (low_price <= high_price),
    close_price NUMERIC(12, 4) NOT NULL CHECK (close_price > 0),
    volume BIGINT NOT NULL CHECK (volume >= 0),
    candle_timestamp TIMESTAMPTZ NOT NULL,
    data_status VARCHAR(16) NOT NULL DEFAULT 'ok' CHECK (data_status IN ('ok', 'degraded')),
    CONSTRAINT uq_candle_ticker_tf_time UNIQUE (ticker, timeframe, candle_timestamp)
);

CREATE INDEX IF NOT EXISTS idx_candles_lookup ON market_candles(ticker, timeframe, candle_timestamp DESC);

-- 4. Trading Signals Table
CREATE TABLE IF NOT EXISTS trade_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_id VARCHAR(64) NOT NULL UNIQUE,
    ticker VARCHAR(16) NOT NULL,
    strategy VARCHAR(32) NOT NULL DEFAULT 'ORB_v1.0',
    strategy_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
    action VARCHAR(8) NOT NULL CHECK (action IN ('BUY', 'SELL')),
    entry_price NUMERIC(12, 4) NOT NULL CHECK (entry_price > 0),
    stop_loss NUMERIC(12, 4) NOT NULL CHECK (stop_loss > 0),
    target_price NUMERIC(12, 4) NOT NULL CHECK (target_price > 0),
    reward_risk_ratio NUMERIC(6, 2) NOT NULL CHECK (reward_risk_ratio >= 1.0),
    position_size INT NOT NULL CHECK (position_size > 0),
    confidence_pct INT NOT NULL CHECK (confidence_pct >= 40 AND confidence_pct <= 75),
    invalidation_reason TEXT NOT NULL,
    data_status VARCHAR(16) NOT NULL DEFAULT 'ok' CHECK (data_status = 'ok'),
    status VARCHAR(32) NOT NULL DEFAULT 'GENERATED' CHECK (status IN ('GENERATED', 'APPROVED', 'REJECTED_BY_RISK', 'EXECUTED', 'CANCELLED')),
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    session_id VARCHAR(64) NOT NULL,
    CONSTRAINT chk_long_stop_loss CHECK (action != 'BUY' OR (stop_loss < entry_price AND target_price > entry_price))
);

-- 5. Daily Halt State (Persisted in DB to survive process crashes)
CREATE TABLE IF NOT EXISTS daily_halts (
    id SERIAL PRIMARY KEY,
    halt_date DATE NOT NULL UNIQUE,
    is_halted BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    triggered_at TIMESTAMPTZ,
    loss_amount NUMERIC(14, 4) DEFAULT 0,
    loss_pct NUMERIC(6, 3) DEFAULT 0
);

-- 6. Demo Account Trades (Hard DB rule: Stop loss CANNOT be null)
CREATE TABLE IF NOT EXISTS demo_trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id VARCHAR(64) NOT NULL UNIQUE,
    signal_id VARCHAR(64) REFERENCES trade_signals(signal_id),
    ticker VARCHAR(16) NOT NULL,
    action VARCHAR(8) NOT NULL CHECK (action IN ('BUY', 'SELL')),
    shares INT NOT NULL CHECK (shares > 0),
    entry_price NUMERIC(12, 4) NOT NULL CHECK (entry_price > 0),
    exit_price NUMERIC(12, 4) CHECK (exit_price IS NULL OR exit_price > 0),
    -- MANDATORY STOP LOSS: Cannot be null, must be below entry for long
    stop_loss NUMERIC(12, 4) NOT NULL CHECK (stop_loss > 0 AND stop_loss < entry_price),
    target_price NUMERIC(12, 4) NOT NULL CHECK (target_price > entry_price),
    slippage_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.0020,
    gross_pnl NUMERIC(14, 4),
    fees_paid NUMERIC(14, 4) NOT NULL DEFAULT 0,
    net_pnl NUMERIC(14, 4),
    risk_pct_used NUMERIC(5, 2) NOT NULL CHECK (risk_pct_used <= 1.00),
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED')),
    exit_reason VARCHAR(32) CHECK (exit_reason IN ('TARGET_HIT', 'STOP_HIT', 'TIME_STOP_1520', 'MANUAL', 'HALT')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    closed_at TIMESTAMPTZ,
    fee_version VARCHAR(32) NOT NULL DEFAULT 'PSX_STANDARD_v1',
    session_id VARCHAR(64) NOT NULL
);

-- 7. Double-Entry Ledger Table
-- Core Invariant: Every transaction has equal debits and credits.
-- Sum of debits - credits across cash and position equity equals starting capital + realized P&L.
CREATE TABLE IF NOT EXISTS demo_ledger (
    id BIGSERIAL PRIMARY KEY,
    transaction_id VARCHAR(64) NOT NULL,
    trade_id VARCHAR(64) REFERENCES demo_trades(trade_id),
    account_name VARCHAR(32) NOT NULL CHECK (account_name IN ('CASH', 'EQUITY_HOLDINGS', 'COMMISSION_EXPENSE', 'TAX_EXPENSE', 'REALIZED_PNL')),
    debit NUMERIC(14, 4) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit NUMERIC(14, 4) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    balance_after NUMERIC(14, 4) NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT chk_debit_or_credit CHECK (debit > 0 OR credit > 0)
);

CREATE INDEX IF NOT EXISTS idx_ledger_tx ON demo_ledger(transaction_id);

-- 8. Journal & Post-Mortem Table
CREATE TABLE IF NOT EXISTS trade_journal (
    id BIGSERIAL PRIMARY KEY,
    trade_id VARCHAR(64) NOT NULL UNIQUE REFERENCES demo_trades(trade_id),
    market_conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
    entry_rationale TEXT NOT NULL,
    exit_rationale TEXT,
    verdict VARCHAR(32) CHECK (verdict IN (
        'Right', 
        'Wrong', 
        'Right-for-wrong-reason', 
        'Wrong-for-right-reason'
    )),
    post_mortem_status VARCHAR(24) NOT NULL DEFAULT 'PENDING' CHECK (post_mortem_status IN ('PENDING', 'COMPLETED', 'FAILED')),
    post_mortem_analysis TEXT,
    transferable_lesson TEXT,
    user_annotation TEXT,
    retry_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- 9. Lessons Memory Table
CREATE TABLE IF NOT EXISTS lessons_memory (
    id SERIAL PRIMARY KEY,
    trade_id VARCHAR(64) REFERENCES demo_trades(trade_id),
    category VARCHAR(32) NOT NULL,
    lesson_text TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    times_cited INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- 10. Mistake Audit Log (Independent from Risk Engine)
CREATE TABLE IF NOT EXISTS mistake_audit_log (
    id BIGSERIAL PRIMARY KEY,
    trade_id VARCHAR(64),
    rule_violated VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('WARNING', 'CRITICAL')),
    details TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE
);

-- 11. Real Portfolio Position Plans
CREATE TABLE IF NOT EXISTS portfolio_plans (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(16) NOT NULL,
    quantity INT NOT NULL CHECK (quantity > 0),
    entry_fill_price NUMERIC(12, 4) NOT NULL CHECK (entry_fill_price > 0),
    -- MANDATORY STOP LOSS: Cannot be null
    stop_loss NUMERIC(12, 4) NOT NULL CHECK (stop_loss > 0 AND stop_loss < entry_fill_price),
    target_price NUMERIC(12, 4) NOT NULL CHECK (target_price > entry_fill_price),
    trim_level NUMERIC(12, 4),
    max_risk_pct NUMERIC(5, 2) NOT NULL,
    oversize_warning BOOLEAN NOT NULL DEFAULT FALSE,
    shares_to_trim INT NOT NULL DEFAULT 0,
    plan_version INT NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CLOSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- 12. System Health & Heartbeats
CREATE TABLE IF NOT EXISTS health_heartbeats (
    id BIGSERIAL PRIMARY KEY,
    component VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('GREEN', 'YELLOW', 'RED')),
    latency_ms NUMERIC(10, 2),
    message TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_health_component_time ON health_heartbeats(component, checked_at DESC);

-- 13. Trades Table (Primary production/demo trade record)
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id VARCHAR(64) NOT NULL UNIQUE,
    signal_id VARCHAR(64),
    ticker VARCHAR(16) NOT NULL,
    action VARCHAR(8) NOT NULL CHECK (action IN ('BUY', 'SELL')),
    shares INT NOT NULL CHECK (shares > 0),
    entry_price NUMERIC(12, 4) NOT NULL CHECK (entry_price > 0),
    exit_price NUMERIC(12, 4) CHECK (exit_price IS NULL OR exit_price > 0),
    stop_loss NUMERIC(12, 4) NOT NULL CHECK (stop_loss > 0 AND stop_loss < entry_price),
    target_price NUMERIC(12, 4) NOT NULL CHECK (target_price > entry_price),
    slippage_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.0020,
    gross_pnl NUMERIC(14, 4),
    fees_paid NUMERIC(14, 4) NOT NULL DEFAULT 0,
    net_pnl NUMERIC(14, 4),
    risk_pct_used NUMERIC(5, 2) NOT NULL CHECK (risk_pct_used <= 1.00),
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED')),
    exit_reason VARCHAR(32) CHECK (exit_reason IN ('TARGET_HIT', 'STOP_HIT', 'TIME_STOP_1520', 'MANUAL', 'HALT')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    closed_at TIMESTAMPTZ,
    fee_version VARCHAR(32) NOT NULL DEFAULT 'PSX_STANDARD_v1',
    session_id VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker_time ON trades(ticker, opened_at DESC);

-- 14. Remote SQL Execution Helper (callable via service_role key RPC)
CREATE OR REPLACE FUNCTION exec_sql(query text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    EXECUTE query;
    RETURN jsonb_build_object('status', 'success');
EXCEPTION WHEN OTHERS THEN
    RETURN jsonb_build_object('status', 'error', 'message', SQLERRM);
END;
$$;

-- 15. Telegram Delivery Log Table
CREATE TABLE IF NOT EXISTS telegram_delivery_log (
    id VARCHAR(64) PRIMARY KEY,
    message_type VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INT NOT NULL DEFAULT 0,
    reference_id VARCHAR(64),
    event_type VARCHAR(64),
    payload TEXT NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    sent_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_telegram_delivery_status ON telegram_delivery_log(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_delivery_ref ON telegram_delivery_log(reference_id);


