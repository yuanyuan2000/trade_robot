CREATE TABLE IF NOT EXISTS intraday_instruments (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    exchange_timezone TEXT NOT NULL DEFAULT 'America/New_York',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minute_bars (
    instrument_id INTEGER NOT NULL,
    minute_utc INTEGER NOT NULL,
    open_scaled INTEGER NOT NULL,
    high_scaled INTEGER NOT NULL,
    low_scaled INTEGER NOT NULL,
    close_scaled INTEGER NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0,
    trade_count INTEGER,
    vwap_scaled INTEGER,
    PRIMARY KEY (instrument_id, minute_utc),
    FOREIGN KEY (instrument_id) REFERENCES intraday_instruments(id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS market_sessions (
    trading_date TEXT PRIMARY KEY,
    open_minute_utc INTEGER NOT NULL,
    close_minute_utc INTEGER NOT NULL,
    is_early_close INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS market_calendar_sync_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT,
    synced_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS minute_sync_state (
    instrument_id INTEGER PRIMARY KEY,
    earliest_minute_utc INTEGER,
    latest_minute_utc INTEGER,
    latest_complete_minute_utc INTEGER,
    row_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (instrument_id) REFERENCES intraday_instruments(id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    next_page_token TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (instrument_id) REFERENCES intraday_instruments(id)
);

CREATE INDEX IF NOT EXISTS idx_import_jobs_instrument_status
ON import_jobs(instrument_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS monthly_fingerprints (
    instrument_id INTEGER NOT NULL,
    year_month TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    first_minute_utc INTEGER NOT NULL,
    last_minute_utc INTEGER NOT NULL,
    digest BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, year_month),
    FOREIGN KEY (instrument_id) REFERENCES intraday_instruments(id)
) WITHOUT ROWID;
