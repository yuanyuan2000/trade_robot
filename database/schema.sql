CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    exchange_name TEXT,
    currency TEXT,
    show_weekend_data INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 0,
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
