from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import struct

from config import INTRADAY_PRICE_SCALE
from database.intraday_db import (
    INTRADAY_WRITE_LOCK,
    get_intraday_connection,
    init_intraday_database,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    return normalized


def iso_to_epoch_minute(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp()) // 60


def epoch_minute_to_iso(value: int) -> str:
    return datetime.fromtimestamp(int(value) * 60, tz=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def scale_price(value) -> int:
    decimal_value = Decimal(str(value)) * INTRADAY_PRICE_SCALE
    return int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def unscale_price(value: int | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(int(value)) / INTRADAY_PRICE_SCALE)


def upsert_instrument(
    symbol: str,
    exchange_timezone: str = "America/New_York",
) -> int:
    init_intraday_database()
    normalized = normalize_symbol(symbol)
    now = utc_now_iso()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            row = conn.execute(
                "SELECT id FROM intraday_instruments WHERE symbol = ?",
                (normalized,),
            ).fetchone()
            if row:
                instrument_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE intraday_instruments
                    SET exchange_timezone = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (exchange_timezone, now, instrument_id),
                )
                return instrument_id

            next_id = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM intraday_instruments"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO intraday_instruments
                    (id, symbol, exchange_timezone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (next_id, normalized, exchange_timezone, now, now),
            )
            conn.execute(
                """
                INSERT INTO minute_sync_state
                    (instrument_id, status, updated_at)
                VALUES (?, 'pending', ?)
                ON CONFLICT(instrument_id) DO NOTHING
                """,
                (next_id, now),
            )
            return next_id


def get_instrument(symbol: str) -> dict | None:
    init_intraday_database()
    normalized = normalize_symbol(symbol)
    with get_intraday_connection() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, exchange_timezone, created_at, updated_at
            FROM intraday_instruments
            WHERE symbol = ?
            """,
            (normalized,),
        ).fetchone()
    return dict(row) if row else None


def upsert_minute_bars(symbol: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    instrument_id = upsert_instrument(symbol)
    payload = []
    for row in rows:
        minute_utc = (
            int(row["minute_utc"])
            if row.get("minute_utc") is not None
            else iso_to_epoch_minute(str(row["timestamp"]))
        )
        payload.append(
            (
                instrument_id,
                minute_utc,
                scale_price(row["open"]),
                scale_price(row["high"]),
                scale_price(row["low"]),
                scale_price(row["close"]),
                int(row.get("volume") or 0),
                int(row["trade_count"]) if row.get("trade_count") is not None else None,
                scale_price(row["vwap"]) if row.get("vwap") is not None else None,
            )
        )

    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.executemany(
                """
                INSERT INTO minute_bars (
                    instrument_id, minute_utc,
                    open_scaled, high_scaled, low_scaled, close_scaled,
                    volume, trade_count, vwap_scaled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, minute_utc) DO UPDATE SET
                    open_scaled = excluded.open_scaled,
                    high_scaled = excluded.high_scaled,
                    low_scaled = excluded.low_scaled,
                    close_scaled = excluded.close_scaled,
                    volume = excluded.volume,
                    trade_count = excluded.trade_count,
                    vwap_scaled = excluded.vwap_scaled
                """,
                payload,
            )
            _update_sync_progress(
                conn,
                instrument_id,
                min(item[1] for item in payload),
                max(item[1] for item in payload),
            )
    return len(payload)


def _update_sync_progress(
    conn,
    instrument_id: int,
    first_minute_utc: int,
    last_minute_utc: int,
) -> None:
    """Update page progress without rescanning all bars for the symbol."""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO minute_sync_state (
            instrument_id, earliest_minute_utc, latest_minute_utc,
            latest_complete_minute_utc, row_count,
            status, last_error, updated_at
        )
        VALUES (?, ?, ?, ?, 0, 'syncing', NULL, ?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            earliest_minute_utc = CASE
                WHEN minute_sync_state.earliest_minute_utc IS NULL
                  OR excluded.earliest_minute_utc < minute_sync_state.earliest_minute_utc
                THEN excluded.earliest_minute_utc
                ELSE minute_sync_state.earliest_minute_utc
            END,
            latest_minute_utc = CASE
                WHEN minute_sync_state.latest_minute_utc IS NULL
                  OR excluded.latest_minute_utc > minute_sync_state.latest_minute_utc
                THEN excluded.latest_minute_utc
                ELSE minute_sync_state.latest_minute_utc
            END,
            latest_complete_minute_utc = CASE
                WHEN minute_sync_state.latest_complete_minute_utc IS NULL
                  OR excluded.latest_complete_minute_utc > minute_sync_state.latest_complete_minute_utc
                THEN excluded.latest_complete_minute_utc
                ELSE minute_sync_state.latest_complete_minute_utc
            END,
            status = 'syncing',
            last_error = NULL,
            updated_at = excluded.updated_at
        """,
        (
            instrument_id,
            int(first_minute_utc),
            int(last_minute_utc),
            int(last_minute_utc),
            now,
        ),
    )


def _refresh_sync_bounds(conn, instrument_id: int, status: str = "success") -> None:
    bounds = conn.execute(
        """
        SELECT MIN(minute_utc), MAX(minute_utc), COUNT(*)
        FROM minute_bars
        WHERE instrument_id = ?
        """,
        (instrument_id,),
    ).fetchone()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO minute_sync_state (
            instrument_id, earliest_minute_utc, latest_minute_utc,
            latest_complete_minute_utc, row_count,
            status, last_error, last_success_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            earliest_minute_utc = excluded.earliest_minute_utc,
            latest_minute_utc = excluded.latest_minute_utc,
            latest_complete_minute_utc = excluded.latest_complete_minute_utc,
            row_count = excluded.row_count,
            status = excluded.status,
            last_error = NULL,
            last_success_at = excluded.last_success_at,
            updated_at = excluded.updated_at
        """,
        (
            instrument_id,
            bounds[0],
            bounds[1],
            bounds[1],
            int(bounds[2] or 0),
            status,
            now if status == "success" else None,
            now,
        ),
    )


def mark_sync_result(symbol: str, status: str, error: str | None = None) -> dict:
    instrument_id = upsert_instrument(symbol)
    now = utc_now_iso()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            if status == "success":
                _refresh_sync_bounds(conn, instrument_id, status="success")
            else:
                conn.execute(
                    """
                    UPDATE minute_sync_state
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE instrument_id = ?
                    """,
                    (status, error, now, instrument_id),
                )
    return get_sync_state(symbol)


def get_sync_state(symbol: str) -> dict:
    instrument = get_instrument(symbol)
    if not instrument:
        return {
            "symbol": normalize_symbol(symbol),
            "status": "not_initialized",
            "row_count": 0,
        }
    with get_intraday_connection() as conn:
        row = conn.execute(
            "SELECT * FROM minute_sync_state WHERE instrument_id = ?",
            (instrument["id"],),
        ).fetchone()
    result = dict(row) if row else {"status": "pending", "row_count": 0}
    result["symbol"] = instrument["symbol"]
    for key in (
        "earliest_minute_utc",
        "latest_minute_utc",
        "latest_complete_minute_utc",
    ):
        result[f"{key.removesuffix('_utc')}_at"] = (
            epoch_minute_to_iso(result[key])
            if result.get(key) is not None
            else None
        )
    return result


def list_instruments() -> list[dict]:
    init_intraday_database()
    with get_intraday_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, exchange_timezone, created_at, updated_at
            FROM intraday_instruments
            ORDER BY symbol
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_storage_quality_summary(symbol: str) -> dict:
    instrument = get_instrument(symbol)
    if not instrument:
        return {
            "symbol": normalize_symbol(symbol),
            "row_count": 0,
            "invalid_ohlc_rows": 0,
            "negative_value_rows": 0,
            "fingerprint_months": 0,
        }
    with get_intraday_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                MIN(minute_utc) AS first_minute_utc,
                MAX(minute_utc) AS last_minute_utc,
                SUM(CASE
                    WHEN low_scaled > high_scaled
                      OR open_scaled < low_scaled
                      OR open_scaled > high_scaled
                      OR close_scaled < low_scaled
                      OR close_scaled > high_scaled
                    THEN 1 ELSE 0 END
                ) AS invalid_ohlc_rows,
                SUM(CASE
                    WHEN volume < 0 OR COALESCE(trade_count, 0) < 0
                    THEN 1 ELSE 0 END
                ) AS negative_value_rows
            FROM minute_bars
            WHERE instrument_id = ?
            """,
            (instrument["id"],),
        ).fetchone()
        fingerprint_months = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM monthly_fingerprints
                WHERE instrument_id = ?
                """,
                (instrument["id"],),
            ).fetchone()[0]
        )
    result = dict(row)
    result["symbol"] = instrument["symbol"]
    result["row_count"] = int(result["row_count"] or 0)
    result["invalid_ohlc_rows"] = int(result["invalid_ohlc_rows"] or 0)
    result["negative_value_rows"] = int(result["negative_value_rows"] or 0)
    result["fingerprint_months"] = fingerprint_months
    result["first_at"] = (
        epoch_minute_to_iso(result["first_minute_utc"])
        if result["first_minute_utc"] is not None
        else None
    )
    result["last_at"] = (
        epoch_minute_to_iso(result["last_minute_utc"])
        if result["last_minute_utc"] is not None
        else None
    )
    return result


def get_minute_bars(
    symbol: str,
    *,
    start_minute: int | None = None,
    end_minute: int | None = None,
    before_minute: int | None = None,
    limit: int = 2000,
    descending: bool = False,
) -> list[dict]:
    instrument = get_instrument(symbol)
    if not instrument:
        return []
    clauses = ["instrument_id = ?"]
    params: list[int] = [int(instrument["id"])]
    if start_minute is not None:
        clauses.append("minute_utc >= ?")
        params.append(int(start_minute))
    if end_minute is not None:
        clauses.append("minute_utc <= ?")
        params.append(int(end_minute))
    if before_minute is not None:
        clauses.append("minute_utc < ?")
        params.append(int(before_minute))
    order = "DESC" if descending else "ASC"
    params.append(max(1, min(int(limit), 20_000)))
    with get_intraday_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT minute_utc, open_scaled, high_scaled, low_scaled,
                   close_scaled, volume, trade_count, vwap_scaled
            FROM minute_bars
            WHERE {' AND '.join(clauses)}
            ORDER BY minute_utc {order}
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            "minute_utc": int(row["minute_utc"]),
            "timestamp": epoch_minute_to_iso(row["minute_utc"]),
            "open": unscale_price(row["open_scaled"]),
            "high": unscale_price(row["high_scaled"]),
            "low": unscale_price(row["low_scaled"]),
            "close": unscale_price(row["close_scaled"]),
            "volume": int(row["volume"]),
            "trade_count": row["trade_count"],
            "vwap": unscale_price(row["vwap_scaled"]),
        }
        for row in rows
    ]


def iter_minute_bars(
    symbol: str,
    *,
    start_minute: int | None = None,
    end_minute: int | None = None,
    batch_size: int = 20_000,
):
    """Yield decoded bars in chronological order without loading all history."""
    instrument = get_instrument(symbol)
    if not instrument:
        return
    clauses = ["instrument_id = ?"]
    params: list[int] = [int(instrument["id"])]
    if start_minute is not None:
        clauses.append("minute_utc >= ?")
        params.append(int(start_minute))
    if end_minute is not None:
        clauses.append("minute_utc <= ?")
        params.append(int(end_minute))
    with get_intraday_connection() as conn:
        cursor = conn.execute(
            f"""
            SELECT minute_utc, open_scaled, high_scaled, low_scaled,
                   close_scaled, volume, trade_count, vwap_scaled
            FROM minute_bars
            WHERE {' AND '.join(clauses)}
            ORDER BY minute_utc ASC
            """,
            params,
        )
        while True:
            rows = cursor.fetchmany(max(1, min(int(batch_size), 50_000)))
            if not rows:
                break
            for row in rows:
                yield {
                    "minute_utc": int(row["minute_utc"]),
                    "timestamp": epoch_minute_to_iso(row["minute_utc"]),
                    "open": unscale_price(row["open_scaled"]),
                    "high": unscale_price(row["high_scaled"]),
                    "low": unscale_price(row["low_scaled"]),
                    "close": unscale_price(row["close_scaled"]),
                    "volume": int(row["volume"]),
                    "trade_count": row["trade_count"],
                    "vwap": unscale_price(row["vwap_scaled"]),
                }


def create_or_resume_import_job(
    symbol: str,
    start_at: str,
    end_at: str | None,
    *,
    feed: str = "sip",
) -> dict:
    instrument_id = upsert_instrument(symbol)
    now = utc_now_iso()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM import_jobs
                WHERE instrument_id = ?
                  AND provider = 'alpaca'
                  AND feed = ?
                  AND start_at = ?
                  AND COALESCE(end_at, '') = COALESCE(?, '')
                  AND status IN ('pending', 'running', 'paused', 'error', 'completed')
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_id, feed, start_at, end_at),
            ).fetchone()
            if row:
                return dict(row)
            row = conn.execute(
                """
                SELECT *
                FROM import_jobs
                WHERE instrument_id = ?
                  AND provider = 'alpaca'
                  AND feed = ?
                  AND start_at = ?
                  AND status IN ('pending', 'running', 'paused', 'error')
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_id, feed, start_at),
            ).fetchone()
            if row:
                return dict(row)
            cursor = conn.execute(
                """
                INSERT INTO import_jobs (
                    instrument_id, provider, feed, start_at, end_at,
                    status, created_at, updated_at
                )
                VALUES (?, 'alpaca', ?, ?, ?, 'pending', ?, ?)
                """,
                (instrument_id, feed, start_at, end_at, now, now),
            )
            job_id = int(cursor.lastrowid)
            return dict(
                conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
            )


def update_import_job(
    job_id: int,
    *,
    status: str,
    next_page_token: str | None = None,
    pages_added: int = 0,
    rows_added: int = 0,
    error: str | None = None,
) -> dict:
    now = utc_now_iso()
    completed_at = now if status == "completed" else None
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(
                """
                UPDATE import_jobs
                SET status = ?,
                    next_page_token = ?,
                    pages_fetched = pages_fetched + ?,
                    rows_written = rows_written + ?,
                    last_error = ?,
                    updated_at = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (
                    status,
                    next_page_token,
                    pages_added,
                    rows_added,
                    error,
                    now,
                    completed_at,
                    int(job_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM import_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
    if not row:
        raise ValueError("Unknown import job")
    return dict(row)


def recompute_monthly_fingerprint(symbol: str, year_month: str) -> dict | None:
    instrument = get_instrument(symbol)
    if not instrument:
        return None
    start_dt = datetime.strptime(year_month, "%Y-%m").replace(tzinfo=timezone.utc)
    end_dt = (
        start_dt.replace(year=start_dt.year + 1, month=1)
        if start_dt.month == 12
        else start_dt.replace(month=start_dt.month + 1)
    )
    start_minute = int(start_dt.timestamp()) // 60
    end_minute = int(end_dt.timestamp()) // 60
    with get_intraday_connection() as conn:
        rows = conn.execute(
            """
            SELECT minute_utc, open_scaled, high_scaled, low_scaled,
                   close_scaled, volume, trade_count, vwap_scaled
            FROM minute_bars
            WHERE instrument_id = ?
              AND minute_utc >= ?
              AND minute_utc < ?
            ORDER BY minute_utc
            """,
            (instrument["id"], start_minute, end_minute),
        ).fetchall()
    if not rows:
        return None

    digest = hashlib.blake2b(digest_size=16)
    for row in rows:
        values = [
            int(row["minute_utc"]),
            int(row["open_scaled"]),
            int(row["high_scaled"]),
            int(row["low_scaled"]),
            int(row["close_scaled"]),
            int(row["volume"]),
            int(row["trade_count"] or 0),
            int(row["vwap_scaled"] or 0),
        ]
        digest.update(struct.pack(">8q", *values))
    result = {
        "instrument_id": int(instrument["id"]),
        "year_month": year_month,
        "row_count": len(rows),
        "first_minute_utc": int(rows[0]["minute_utc"]),
        "last_minute_utc": int(rows[-1]["minute_utc"]),
        "digest": digest.digest(),
        "updated_at": utc_now_iso(),
    }
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(
                """
                INSERT INTO monthly_fingerprints (
                    instrument_id, year_month, row_count,
                    first_minute_utc, last_minute_utc, digest, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, year_month) DO UPDATE SET
                    row_count = excluded.row_count,
                    first_minute_utc = excluded.first_minute_utc,
                    last_minute_utc = excluded.last_minute_utc,
                    digest = excluded.digest,
                    updated_at = excluded.updated_at
                """,
                tuple(result.values()),
            )
    return {**result, "digest": result["digest"].hex()}
