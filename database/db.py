from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

from config import DATA_DIR, DATABASE_PATH, INTRADAY_DATABASE_PATH, SCHEMA_PATH
from database.intraday_db import (
    INTRADAY_WRITE_LOCK,
    checkpoint_intraday_database,
    init_intraday_database,
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    schema = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(schema)
        migrate_database(conn)
    from database.repository import seed_default_indicators, seed_symbol_aliases

    seed_symbol_aliases()
    seed_default_indicators()


def migrate_database(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "backtest_runs", "termination_reason", "TEXT")
    ensure_column(conn, "backtest_runs", "configuration_summary", "TEXT")
    ensure_column(conn, "backtest_runs", "log_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "backtest_runs", "log_bytes", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "backtest_runs", "logs_deleted_at", "TEXT")
    ensure_column(conn, "backtest_runs", "deleted_at", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backtest_runs_deleted
        ON backtest_runs(deleted_at, created_at DESC)
        """
    )
    # Older versions only hid deleted runs and retained their compact summary
    # and trades. Finish those already-requested deletions during migration.
    for table in ("backtest_logs", "backtest_equity_points", "backtest_trades"):
        conn.execute(
            f"""
            DELETE FROM {table}
            WHERE run_id IN (
                SELECT id FROM backtest_runs WHERE deleted_at IS NOT NULL
            )
            """
        )
    conn.execute("DELETE FROM backtest_runs WHERE deleted_at IS NOT NULL")
    conn.execute(
        """
        UPDATE backtest_runs
        SET log_count = (
                SELECT COUNT(*) FROM backtest_logs
                WHERE backtest_logs.run_id = backtest_runs.id
            ),
            log_bytes = (
                SELECT COALESCE(SUM(
                    LENGTH(message) + LENGTH(COALESCE(context_json, ''))
                ), 0)
                FROM backtest_logs
                WHERE backtest_logs.run_id = backtest_runs.id
            )
        WHERE logs_deleted_at IS NULL
          AND log_count = 0
        """
    )
    ensure_column(
        conn, "backtest_equity_points", "borrowed_cash", "REAL NOT NULL DEFAULT 0"
    )
    ensure_column(
        conn, "backtest_equity_points", "gross_leverage", "REAL NOT NULL DEFAULT 0"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            common_symbol TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            yahoo_symbol TEXT,
            twelvedata_symbol TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_symbol_aliases_yahoo
        ON symbol_aliases(yahoo_symbol)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_symbol_aliases_twelvedata
        ON symbol_aliases(twelvedata_symbol)
        """
    )
    had_show_non_us_market_days = any(
        row["name"] == "show_non_us_market_days"
        for row in conn.execute('PRAGMA table_info("symbols")').fetchall()
    )
    ensure_column(
        conn,
        table_name="symbols",
        column_name="show_weekend_data",
        definition="INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(
        conn,
        table_name="symbols",
        column_name="show_in_overview",
        definition="INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(
        conn,
        table_name="symbols",
        column_name="display_order",
        definition="INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(
        conn,
        table_name="symbols",
        column_name="show_non_us_market_days",
        definition="INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(conn, "symbols", "alpaca_symbol", "TEXT")
    ensure_column(conn, "symbols", "alpaca_asset_id", "TEXT")
    ensure_column(conn, "symbols", "alpaca_supported", "INTEGER")
    ensure_column(conn, "symbols", "alpaca_checked_at", "TEXT")
    ensure_column(conn, "symbols", "alpaca_error", "TEXT")
    ensure_column(
        conn,
        "symbols",
        "asset_class",
        "TEXT NOT NULL DEFAULT 'us_equity'",
    )
    ensure_column(conn, "symbols", "quantity_step", "REAL")
    ensure_column(conn, "symbols", "cusip", "TEXT")
    ensure_column(conn, "symbols", "isin", "TEXT")
    ensure_column(conn, "symbols", "history_start_date", "TEXT")
    ensure_column(conn, "symbols", "history_start_source", "TEXT")
    ensure_column(
        conn,
        "symbols",
        "history_start_verified",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(conn, "symbols", "daily_history_start_date", "TEXT")
    ensure_column(conn, "symbols", "daily_history_start_source", "TEXT")
    ensure_column(
        conn,
        "symbols",
        "daily_history_start_verified",
        "INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        """
        UPDATE symbols
        SET display_order = id
        WHERE display_order IS NULL OR display_order = 0
        """
    )
    if not had_show_non_us_market_days:
        conn.execute(
            """
            UPDATE symbols
            SET show_non_us_market_days = show_weekend_data
            """
        )
    ensure_column(
        conn,
        table_name="daily_prices",
        column_name="updated_at",
        definition="TEXT",
    )
    ensure_column(conn, "daily_prices", "source_provider", "TEXT")
    ensure_column(conn, "daily_prices", "source_timeframe", "TEXT")
    ensure_column(
        conn,
        "daily_prices",
        "price_basis",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    ensure_column(
        conn,
        "daily_prices",
        "is_complete",
        "INTEGER NOT NULL DEFAULT 1",
    )
    conn.execute(
        """
        UPDATE daily_prices
        SET updated_at = created_at
        WHERE updated_at IS NULL OR updated_at = ''
        """
    )
    conn.execute(
        """
        UPDATE daily_prices
        SET price_basis = 'raw'
        WHERE source_provider = 'alpaca'
          AND (price_basis IS NULL OR price_basis = 'unknown')
        """
    )
    conn.executemany(
        "UPDATE symbols SET asset_class = ? WHERE symbol = ?",
        [
            ("index", "USDINDEX"),
            ("index", "SPX"),
            ("forex", "XAU/USD"),
            ("fixed_income", "US10Y"),
        ],
    )
    conn.execute(
        """
        UPDATE symbols
        SET history_start_date = (
                SELECT MIN(daily_prices.date)
                FROM daily_prices
                WHERE daily_prices.symbol = symbols.symbol
            ),
            history_start_source = CASE
                WHEN history_start_source IS NULL THEN 'daily_prices'
                ELSE history_start_source
            END,
            history_start_verified = 1
        WHERE EXISTS (
            SELECT 1 FROM daily_prices
            WHERE daily_prices.symbol = symbols.symbol
        )
          AND (
              history_start_date IS NULL
              OR history_start_date > (
                  SELECT MIN(daily_prices.date)
                  FROM daily_prices
                  WHERE daily_prices.symbol = symbols.symbol
              )
          )
        """
    )
    conn.execute(
        """
        UPDATE symbols
        SET daily_history_start_date = history_start_date,
            daily_history_start_source = history_start_source,
            daily_history_start_verified = history_start_verified
        WHERE history_start_date IS NOT NULL
          AND (
              daily_history_start_date IS NULL
              OR history_start_date < daily_history_start_date
          )
        """
    )
    conn.executescript(
        """
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
            price_basis TEXT NOT NULL DEFAULT 'unknown',
            is_complete INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(symbol, series_code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_price_series_lookup
        ON daily_price_series(symbol, series_code, date);
        INSERT OR IGNORE INTO daily_price_series (
            symbol, series_code, date, open, high, low, close, volume,
            source_provider, source_timeframe, price_basis, is_complete,
            created_at, updated_at
        )
        SELECT symbol, 'US_EQUITY_SESSION', date, open, high, low, close, volume,
               source_provider, source_timeframe, price_basis, is_complete,
               created_at, updated_at
        FROM daily_prices
        WHERE source_timeframe = 'nyse_session_derived_1m';
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
        """
    )
    for column_name, definition in (
        ("currency", "TEXT"),
        ("region", "TEXT"),
        ("sub_type", "TEXT"),
        ("special", "INTEGER NOT NULL DEFAULT 0"),
        ("foreign_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("due_bill_on_date", "TEXT"),
        ("due_bill_off_date", "TEXT"),
        ("effective_date", "TEXT"),
        ("event_status", "TEXT NOT NULL DEFAULT 'active'"),
        ("instrument_key", "TEXT"),
        ("identity_status", "TEXT NOT NULL DEFAULT 'unresolved'"),
        ("first_seen_at", "TEXT"),
        ("last_seen_at", "TEXT"),
    ):
        ensure_column(conn, "corporate_actions", column_name, definition)
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trendline_snapshots_lookup
        ON trendline_analysis_snapshots(
            symbol,
            period,
            window_size,
            algorithm_version,
            computed_at DESC
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_key_zone_snapshots_lookup
        ON key_zone_analysis_snapshots(
            symbol,
            period,
            window_size,
            show_weekend_data,
            adjustment,
            algorithm_version,
            computed_at DESC
        )
        """
    )
    ensure_column(
        conn,
        table_name="backtest_equity_points",
        column_name="receivables",
        definition="REAL NOT NULL DEFAULT 0",
    )
    ensure_column(
        conn,
        table_name="backtest_strategies",
        column_name="market_json",
        definition=(
            "TEXT NOT NULL DEFAULT "
            "'{\"calendar\":\"XNYS\",\"timezone\":\"America/New_York\","
            "\"type\":\"US_EQUITY\"}'"
        ),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_strategy_seed_state (
            seed_key TEXT PRIMARY KEY,
            strategy_id INTEGER,
            seeded_at TEXT NOT NULL
        )
        """
    )
    ensure_column(
        conn,
        "realtime_decision_tasks",
        "panel_settings_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    ensure_column(
        conn,
        "realtime_decision_tasks",
        "panel_revision",
        "INTEGER NOT NULL DEFAULT 1",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_task_seed_state (
            seed_key TEXT PRIMARY KEY,
            task_id INTEGER,
            seeded_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES realtime_decision_tasks(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            setting_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }
    if column_name not in columns:
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {definition}')


def backup_database() -> Path:
    return Path(backup_databases(["main"])[0]["path"])


def backup_databases(targets: list[str]) -> list[dict]:
    normalized = []
    for target in targets:
        clean_target = str(target).strip().lower()
        if clean_target not in {"main", "intraday"}:
            raise ValueError(f"Unknown database target: {target}")
        if clean_target not in normalized:
            normalized.append(clean_target)
    if not normalized:
        raise ValueError("At least one database target is required")

    if "main" in normalized:
        init_database()
    if "intraday" in normalized:
        init_intraday_database()
        checkpoint_intraday_database("FULL")

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sources = {
        "main": Path(DATABASE_PATH),
        "intraday": Path(INTRADAY_DATABASE_PATH),
    }
    filenames = {
        "main": f"market_data_backup_{timestamp}.sqlite",
        "intraday": f"intraday_data_backup_{timestamp}.sqlite",
    }
    required_bytes = sum(sources[target].stat().st_size for target in normalized)
    free_bytes = shutil.disk_usage(backup_dir).free
    if free_bytes < required_bytes + 50 * 1024 * 1024:
        raise OSError(
            f"Insufficient disk space: need at least {required_bytes} bytes plus reserve."
        )

    results = []
    lock = INTRADAY_WRITE_LOCK if "intraday" in normalized else _NullLock()
    with lock:
        for target_name in normalized:
            backup_path = backup_dir / filenames[target_name]
            source = sqlite3.connect(sources[target_name])
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination, pages=2048)
                check = destination.execute("PRAGMA quick_check").fetchone()
                quick_check = str(check[0]) if check else "unknown"
            finally:
                destination.close()
                source.close()
            if quick_check != "ok":
                raise sqlite3.DatabaseError(
                    f"{target_name} backup quick_check failed: {quick_check}"
                )
            results.append(
                {
                    "target": target_name,
                    "path": str(backup_path),
                    "filename": backup_path.name,
                    "size_bytes": backup_path.stat().st_size,
                    "quick_check": quick_check,
                }
            )

    return results


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None
