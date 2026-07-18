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
    from database.repository import seed_default_indicators

    seed_default_indicators()


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
