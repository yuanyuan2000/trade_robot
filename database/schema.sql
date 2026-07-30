CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    exchange_name TEXT,
    currency TEXT,
    show_weekend_data INTEGER NOT NULL DEFAULT 1,
    show_in_overview INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    alpaca_symbol TEXT,
    alpaca_supported INTEGER,
    alpaca_checked_at TEXT,
    alpaca_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0,
    source_provider TEXT,
    source_timeframe TEXT,
    is_complete INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_symbol_date
ON daily_prices(symbol, date);

CREATE TABLE IF NOT EXISTS symbol_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    common_symbol TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    yahoo_symbol TEXT,
    twelvedata_symbol TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbol_aliases_yahoo
ON symbol_aliases(yahoo_symbol);

CREATE INDEX IF NOT EXISTS idx_symbol_aliases_twelvedata
ON symbol_aliases(twelvedata_symbol);

CREATE TABLE IF NOT EXISTS api_request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    symbol TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    indicator_type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_indicators_unique_config
ON indicators(indicator_type, params_json);

CREATE TABLE IF NOT EXISTS symbol_chart_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    view_code TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_value INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol_id, view_code),
    FOREIGN KEY(symbol_id) REFERENCES symbols(id)
);

CREATE INDEX IF NOT EXISTS idx_symbol_chart_views_symbol
ON symbol_chart_views(symbol_id, view_code);

CREATE TABLE IF NOT EXISTS symbol_indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    chart_view_id INTEGER NOT NULL,
    view_code TEXT NOT NULL,
    indicator_id INTEGER NOT NULL,
    color TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(symbol_id) REFERENCES symbols(id),
    FOREIGN KEY(chart_view_id) REFERENCES symbol_chart_views(id),
    FOREIGN KEY(indicator_id) REFERENCES indicators(id),
    UNIQUE(symbol_id, view_code, indicator_id)
);

CREATE INDEX IF NOT EXISTS idx_symbol_indicators_view
ON symbol_indicators(symbol_id, view_code, sort_order);

CREATE TABLE IF NOT EXISTS trendline_analysis_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    window_size INTEGER NOT NULL,
    show_weekend_data INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    latest_data_date TEXT,
    data_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(
        symbol,
        period,
        window_size,
        show_weekend_data,
        algorithm_version,
        data_fingerprint
    )
);

CREATE INDEX IF NOT EXISTS idx_trendline_snapshots_lookup
ON trendline_analysis_snapshots(
    symbol,
    period,
    window_size,
    algorithm_version,
    computed_at DESC
);

CREATE TABLE IF NOT EXISTS backtest_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    design_mode TEXT NOT NULL CHECK(design_mode IN ('visual', 'code')),
    selection_mode TEXT NOT NULL CHECK(selection_mode IN ('single', 'distribution', 'competition')),
    code_key TEXT,
    code_version TEXT,
    definition_json TEXT NOT NULL,
    default_settings_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK(
        (design_mode = 'visual' AND code_key IS NULL)
        OR
        (design_mode = 'code' AND code_key IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategies_active
ON backtest_strategies(deleted_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS backtest_strategy_seed_state (
    seed_key TEXT PRIMARY KEY,
    strategy_id INTEGER,
    seeded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER,
    strategy_name TEXT NOT NULL,
    strategy_revision INTEGER NOT NULL,
    strategy_snapshot_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    data_manifest_json TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'validating', 'running', 'cancelling',
        'completed', 'failed', 'cancelled'
    )),
    progress REAL NOT NULL DEFAULT 0,
    current_time TEXT,
    metrics_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(strategy_id) REFERENCES backtest_strategies(id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy
ON backtest_runs(strategy_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_status
ON backtest_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_equity_points (
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    trading_date TEXT NOT NULL,
    cash REAL NOT NULL,
    receivables REAL NOT NULL DEFAULT 0,
    positions_value REAL NOT NULL,
    equity REAL NOT NULL,
    return_rate REAL NOT NULL,
    drawdown_rate REAL NOT NULL,
    benchmark_equity REAL,
    benchmark_return_rate REAL,
    positions_json TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_date
ON backtest_equity_points(run_id, trading_date);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    quantity REAL NOT NULL,
    reference_price REAL NOT NULL,
    fill_price REAL NOT NULL,
    gross_amount REAL NOT NULL,
    commission REAL NOT NULL,
    slippage_amount REAL NOT NULL,
    realized_pnl REAL,
    cash_after REAL NOT NULL,
    position_quantity_after REAL NOT NULL,
    position_value_after REAL NOT NULL,
    position_weight_after REAL NOT NULL,
    reason TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id),
    UNIQUE(run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_time
ON backtest_trades(run_id, event_time, sequence);

CREATE TABLE IF NOT EXISTS backtest_logs (
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    event_time TEXT,
    level TEXT NOT NULL CHECK(level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    event_type TEXT NOT NULL,
    symbol TEXT,
    message TEXT NOT NULL,
    context_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_logs_filter
ON backtest_logs(run_id, level, sequence);

CREATE TABLE IF NOT EXISTS corporate_actions (
    provider_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    action_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    process_date TEXT NOT NULL,
    ex_date TEXT,
    record_date TEXT,
    payable_date TEXT,
    old_rate REAL,
    new_rate REAL,
    cash_rate REAL,
    payload_json TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date
ON corporate_actions(symbol, ex_date, process_date);

CREATE TABLE IF NOT EXISTS corporate_action_sync_state (
    symbol TEXT PRIMARY KEY,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT,
    synced_at TEXT NOT NULL
);
