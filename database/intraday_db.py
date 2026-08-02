from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import threading

from config import DATA_DIR, INTRADAY_DATABASE_PATH, INTRADAY_SCHEMA_PATH


INTRADAY_WRITE_LOCK = threading.RLock()
INTRADAY_PAGE_SIZE = 8192


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def get_intraday_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database_exists = Path(INTRADAY_DATABASE_PATH).exists()
    conn = sqlite3.connect(
        INTRADAY_DATABASE_PATH,
        timeout=30,
        factory=ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not database_exists:
        conn.execute(f"PRAGMA page_size = {INTRADAY_PAGE_SIZE}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_intraday_database() -> None:
    schema = Path(INTRADAY_SCHEMA_PATH).read_text(encoding="utf-8")
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.executescript(schema)
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(intraday_instruments)"
                ).fetchall()
            }
            if "asset_class" not in columns:
                conn.execute(
                    "ALTER TABLE intraday_instruments "
                    "ADD COLUMN asset_class TEXT NOT NULL DEFAULT 'us_equity'"
                )


def checkpoint_intraday_database(mode: str = "FULL") -> None:
    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError("Unsupported checkpoint mode")
    init_intraday_database()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()


def intraday_quick_check(path: Path | None = None) -> str:
    target = Path(path or INTRADAY_DATABASE_PATH)
    if not target.exists():
        init_intraday_database()
    conn = sqlite3.connect(target)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "unknown"
    finally:
        conn.close()


def intraday_database_info() -> dict:
    init_intraday_database()
    path = Path(INTRADAY_DATABASE_PATH)
    with get_intraday_connection() as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        bar_count = int(conn.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "page_size": page_size,
        "page_count": page_count,
        "bar_count": bar_count,
        "checked_at": datetime.now().astimezone().isoformat(),
    }
