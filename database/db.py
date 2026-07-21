from __future__ import annotations

from datetime import datetime
import sqlite3
from pathlib import Path

from config import DATA_DIR, DATABASE_PATH, SCHEMA_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
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
    conn.execute(
        """
        UPDATE daily_prices
        SET updated_at = created_at
        WHERE updated_at IS NULL OR updated_at = ''
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
    if not DATABASE_PATH.exists():
        init_database()

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"market_data_backup_{timestamp}.sqlite"

    with get_connection() as source:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()

    return backup_path
