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
    conn = sqlite3.connect(DATABASE_PATH, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
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
    ensure_column(conn, "symbols", "alpaca_symbol", "TEXT")
    ensure_column(conn, "symbols", "alpaca_supported", "INTEGER")
    ensure_column(conn, "symbols", "alpaca_checked_at", "TEXT")
    ensure_column(conn, "symbols", "alpaca_error", "TEXT")
    conn.execute(
        """
        UPDATE symbols
        SET display_order = id
        WHERE display_order IS NULL OR display_order = 0
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
