CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    exchange_name TEXT,
    currency TEXT,
    show_weekend_data INTEGER NOT NULL DEFAULT 1,
    show_non_us_market_days INTEGER NOT NULL DEFAULT 1,
    show_in_overview INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    alpaca_symbol TEXT,
    alpaca_asset_id TEXT,
    alpaca_supported INTEGER,
    alpaca_checked_at TEXT,
    alpaca_error TEXT,
    asset_class TEXT NOT NULL DEFAULT 'us_equity',
    cusip TEXT,
    isin TEXT,
    quantity_step REAL,
    history_start_date TEXT,
    history_start_source TEXT,
    history_start_verified INTEGER NOT NULL DEFAULT 0,
    daily_history_start_date TEXT,
    daily_history_start_source TEXT,
    daily_history_start_verified INTEGER NOT NULL DEFAULT 0,
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
    price_basis TEXT NOT NULL DEFAULT 'unknown' CHECK(price_basis IN (
        'raw', 'split_adjusted', 'total_return_adjusted', 'unknown'
    )),
    is_complete INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_symbol_date
ON daily_prices(symbol, date);

CREATE TABLE IF NOT EXISTS daily_price_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    series_code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0,
    source_provider TEXT,
    source_timeframe TEXT,
    price_basis TEXT NOT NULL DEFAULT 'unknown' CHECK(price_basis IN (
        'raw', 'split_adjusted', 'total_return_adjusted', 'unknown'
    )),
    is_complete INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, series_code, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_price_series_lookup
ON daily_price_series(symbol, series_code, date);

CREATE TABLE IF NOT EXISTS instrument_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'provider',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(instrument_key, symbol, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_instrument_symbols_lookup
ON instrument_symbols(symbol, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS instrument_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'provider',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(identifier_type, identifier_value, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_instrument_identifiers_lookup
ON instrument_identifiers(identifier_type, identifier_value, valid_from, valid_to);

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

CREATE TABLE IF NOT EXISTS key_zone_analysis_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    window_size INTEGER NOT NULL,
    show_weekend_data INTEGER NOT NULL,
    adjustment TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    latest_data_date TEXT,
    data_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(
        symbol,
        period,
        window_size,
        show_weekend_data,
        adjustment,
        algorithm_version,
        data_fingerprint
    )
);

CREATE INDEX IF NOT EXISTS idx_key_zone_snapshots_lookup
ON key_zone_analysis_snapshots(
    symbol,
    period,
    window_size,
    show_weekend_data,
    adjustment,
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
    market_json TEXT NOT NULL DEFAULT '{"calendar":"XNYS","timezone":"America/New_York","type":"US_EQUITY"}',
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
    termination_reason TEXT,
    configuration_summary TEXT,
    log_count INTEGER NOT NULL DEFAULT 0,
    log_bytes INTEGER NOT NULL DEFAULT 0,
    logs_deleted_at TEXT,
    deleted_at TEXT,
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
    borrowed_cash REAL NOT NULL DEFAULT 0,
    gross_leverage REAL NOT NULL DEFAULT 0,
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

-- Live decision tasks keep calculation snapshots and notification audits
-- separate from immutable historical backtest runs.
CREATE TABLE IF NOT EXISTS realtime_decision_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    strategy_id INTEGER NOT NULL,
    follow_strategy INTEGER NOT NULL DEFAULT 1,
    source_strategy_revision INTEGER NOT NULL,
    source_code_version TEXT,
    strategy_snapshot_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    panel_settings_json TEXT NOT NULL DEFAULT '{}',
    panel_revision INTEGER NOT NULL DEFAULT 1,
    notification_settings_json TEXT NOT NULL,
    portfolio_state_json TEXT NOT NULL DEFAULT '{}',
    desired_state TEXT NOT NULL DEFAULT 'stopped'
        CHECK(desired_state IN ('stopped', 'running')),
    runtime_state TEXT NOT NULL DEFAULT 'stopped'
        CHECK(runtime_state IN ('stopped', 'starting', 'running', 'degraded', 'stopping', 'error')),
    run_started_at TEXT,
    stopped_at TEXT,
    heartbeat_at TEXT,
    next_event_at TEXT,
    last_event_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    successful_notification_count INTEGER NOT NULL DEFAULT 0,
    next_allowed_normal_send_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(strategy_id) REFERENCES backtest_strategies(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_realtime_tasks_active
ON realtime_decision_tasks(deleted_at, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_realtime_tasks_runtime
ON realtime_decision_tasks(runtime_state, desired_state, next_event_at);

CREATE TABLE IF NOT EXISTS realtime_task_seed_state (
    seed_key TEXT PRIMARY KEY,
    task_id INTEGER,
    seeded_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES realtime_decision_tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS realtime_decision_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    strategy_snapshot_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    notification_settings_json TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'starting'
        CHECK(status IN ('starting', 'running', 'degraded', 'stopping', 'stopped', 'failed')),
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    heartbeat_at TEXT,
    last_event_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES realtime_decision_tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_realtime_runs_task
ON realtime_decision_runs(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS realtime_decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    trading_date TEXT NOT NULL,
    event_name TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'completed', 'skipped', 'failed')),
    data_manifest_json TEXT,
    decision_json TEXT,
    calculation_json TEXT,
    message_subject TEXT,
    message_body TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES realtime_decision_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES realtime_decision_tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_realtime_events_task_time
ON realtime_decision_events(task_id, scheduled_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS email_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    provider TEXT NOT NULL CHECK(provider IN ('gmail_smtp', 'qq_smtp', 'custom_smtp')),
    sender_email TEXT NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    security_mode TEXT NOT NULL CHECK(security_mode IN ('ssl', 'starttls')),
    username TEXT NOT NULL,
    secret_ciphertext TEXT,
    secret_key_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_test_at TEXT,
    last_test_ok INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS realtime_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'sending', 'sent', 'retrying', 'failed', 'suppressed')),
    is_retry INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    sent_at TEXT,
    provider_message_id TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES realtime_decision_events(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES realtime_decision_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(channel_id) REFERENCES email_channels(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_realtime_notifications_queue
ON realtime_notifications(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS realtime_notification_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('sent', 'retrying', 'failed')),
    provider_message_id TEXT,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY(notification_id) REFERENCES realtime_notifications(id) ON DELETE CASCADE,
    UNIQUE(notification_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_realtime_notification_attempts_notification
ON realtime_notification_attempts(notification_id, attempt_number);

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
    currency TEXT,
    region TEXT,
    sub_type TEXT,
    special INTEGER NOT NULL DEFAULT 0,
    foreign_flag INTEGER NOT NULL DEFAULT 0,
    due_bill_on_date TEXT,
    due_bill_off_date TEXT,
    effective_date TEXT,
    event_status TEXT NOT NULL DEFAULT 'active',
    instrument_key TEXT,
    identity_status TEXT NOT NULL DEFAULT 'unresolved',
    first_seen_at TEXT,
    last_seen_at TEXT,
    payload_json TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date
ON corporate_actions(symbol, ex_date, process_date);

CREATE TABLE IF NOT EXISTS corporate_action_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    role TEXT NOT NULL,
    instrument_key TEXT,
    symbol TEXT,
    cusip TEXT,
    isin TEXT,
    share_rate REAL,
    cash_rate REAL,
    currency TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(provider_id) REFERENCES corporate_actions(provider_id),
    UNIQUE(provider_id, role, symbol, cusip, isin)
);

CREATE INDEX IF NOT EXISTS idx_corporate_action_legs_lookup
ON corporate_action_legs(symbol, cusip, isin, role);

CREATE TABLE IF NOT EXISTS corporate_action_sync_state (
    symbol TEXT PRIMARY KEY,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT,
    synced_at TEXT NOT NULL
);
