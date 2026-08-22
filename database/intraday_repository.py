from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import struct

from config import INTRADAY_PRICE_SCALE, KNOWN_MINUTE_HISTORY_STARTS
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
    asset_class: str | None = None,
) -> int:
    init_intraday_database()
    normalized = normalize_symbol(symbol)
    known_start = KNOWN_MINUTE_HISTORY_STARTS.get(normalized)
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
                    SET exchange_timezone = ?,
                        asset_class = COALESCE(?, asset_class),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (exchange_timezone, asset_class, now, instrument_id),
                )
                if known_start:
                    conn.execute(
                        """
                        UPDATE intraday_instruments
                        SET minute_history_start_date = ?,
                            minute_history_start_source = ?,
                            minute_history_start_verified = 1
                        WHERE id = ?
                        """,
                        (
                            known_start["date"],
                            known_start["source"],
                            instrument_id,
                        ),
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
                    (id, symbol, exchange_timezone, asset_class,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    next_id,
                    normalized,
                    exchange_timezone,
                    asset_class or "us_equity",
                    now,
                    now,
                ),
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
            if known_start:
                conn.execute(
                    """
                    UPDATE intraday_instruments
                    SET minute_history_start_date = ?,
                        minute_history_start_source = ?,
                        minute_history_start_verified = 1
                    WHERE id = ?
                    """,
                    (
                        known_start["date"],
                        known_start["source"],
                        next_id,
                    ),
                )
            return next_id


def get_instrument(symbol: str) -> dict | None:
    init_intraday_database()
    normalized = normalize_symbol(symbol)
    with get_intraday_connection() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, exchange_timezone, asset_class,
                   minute_history_start_date,
                   minute_history_start_source,
                   minute_history_start_verified,
                   created_at, updated_at
            FROM intraday_instruments
            WHERE symbol = ?
            """,
            (normalized,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["minute_history_start_verified"] = bool(
        result["minute_history_start_verified"]
    )
    return result


def mark_minute_history_start(
    symbol: str,
    history_start_date: str,
    *,
    source: str,
    verified: bool = True,
) -> dict:
    normalized = normalize_symbol(symbol)
    datetime.fromisoformat(str(history_start_date))
    instrument_id = upsert_instrument(normalized)
    now = utc_now_iso()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(
                """
                UPDATE intraday_instruments
                SET minute_history_start_date = CASE
                        WHEN minute_history_start_date IS NULL
                          OR ? < minute_history_start_date
                        THEN ?
                        ELSE minute_history_start_date
                    END,
                    minute_history_start_source = CASE
                        WHEN minute_history_start_date IS NULL
                          OR ? <= minute_history_start_date
                        THEN ?
                        ELSE minute_history_start_source
                    END,
                    minute_history_start_verified = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    history_start_date,
                    history_start_date,
                    history_start_date,
                    source,
                    1 if verified else 0,
                    now,
                    instrument_id,
                ),
            )
    return get_instrument(normalized)


def upsert_minute_bars(
    symbol: str,
    rows: list[dict],
    *,
    asset_class: str = "us_equity",
) -> int:
    if not rows:
        return 0
    instrument_id = upsert_instrument(symbol, asset_class=asset_class)
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
                float(row.get("volume") or 0),
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
    result["minute_history_start_date"] = instrument.get(
        "minute_history_start_date"
    )
    result["minute_history_start_source"] = instrument.get(
        "minute_history_start_source"
    )
    result["minute_history_start_verified"] = bool(
        instrument.get("minute_history_start_verified")
    )
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


def upsert_market_sessions(
    sessions: list[dict],
    *,
    coverage_start: str,
    coverage_end: str,
) -> None:
    now = utc_now_iso()
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(
                """
                DELETE FROM market_sessions
                WHERE trading_date >= ? AND trading_date <= ?
                """,
                (coverage_start, coverage_end),
            )
            conn.executemany(
                """
                INSERT INTO market_sessions (
                    trading_date, open_minute_utc, close_minute_utc,
                    is_early_close, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trading_date) DO UPDATE SET
                    open_minute_utc = excluded.open_minute_utc,
                    close_minute_utc = excluded.close_minute_utc,
                    is_early_close = excluded.is_early_close,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        item["trading_date"],
                        int(item["open_minute_utc"]),
                        int(item["close_minute_utc"]),
                        int(bool(item.get("is_early_close"))),
                        now,
                    )
                    for item in sessions
                ],
            )
            conn.execute(
                """
                INSERT INTO market_calendar_sync_state (
                    id, coverage_start, coverage_end, status, last_error, synced_at
                )
                VALUES (1, ?, ?, 'success', NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    coverage_start = MIN(
                        market_calendar_sync_state.coverage_start,
                        excluded.coverage_start
                    ),
                    coverage_end = MAX(
                        market_calendar_sync_state.coverage_end,
                        excluded.coverage_end
                    ),
                    status = 'success',
                    last_error = NULL,
                    synced_at = excluded.synced_at
                """,
                (coverage_start, coverage_end, now),
            )


def mark_market_calendar_sync_error(
    *,
    coverage_start: str,
    coverage_end: str,
    error: str,
) -> None:
    with INTRADAY_WRITE_LOCK:
        with get_intraday_connection() as conn:
            conn.execute(
                """
                INSERT INTO market_calendar_sync_state (
                    id, coverage_start, coverage_end, status, last_error, synced_at
                )
                VALUES (1, ?, ?, 'error', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = CASE
                        WHEN market_calendar_sync_state.status = 'success'
                        THEN 'success'
                        ELSE 'error'
                    END,
                    last_error = excluded.last_error,
                    synced_at = excluded.synced_at
                """,
                (coverage_start, coverage_end, error, utc_now_iso()),
            )


def get_market_calendar_coverage() -> dict | None:
    with get_intraday_connection() as conn:
        row = conn.execute(
            "SELECT * FROM market_calendar_sync_state WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None


def get_market_sessions(start_date: str, end_date: str) -> list[dict]:
    with get_intraday_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM market_sessions
            WHERE trading_date >= ? AND trading_date <= ?
            ORDER BY trading_date
            """,
            (start_date, end_date),
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
            "volume": float(row["volume"]),
            "trade_count": row["trade_count"],
            "vwap": unscale_price(row["vwap_scaled"]),
        }
        for row in rows
    ]


def get_minute_bars_at(
    symbol: str,
    minute_values: list[int],
) -> dict[int, dict]:
    """Return exact stored minutes without scanning an instrument's full history."""
    instrument = get_instrument(symbol)
    requested = sorted({int(value) for value in minute_values})
    if not instrument or not requested:
        return {}
    result: dict[int, dict] = {}
    with get_intraday_connection() as conn:
        for start in range(0, len(requested), 800):
            chunk = requested[start : start + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT minute_utc, open_scaled, high_scaled, low_scaled,
                       close_scaled, volume, trade_count, vwap_scaled
                FROM minute_bars
                WHERE instrument_id = ?
                  AND minute_utc IN ({placeholders})
                """,
                (int(instrument["id"]), *chunk),
            ).fetchall()
            for row in rows:
                minute = int(row["minute_utc"])
                result[minute] = {
                    "minute_utc": minute,
                    "timestamp": epoch_minute_to_iso(minute),
                    "open": unscale_price(row["open_scaled"]),
                    "high": unscale_price(row["high_scaled"]),
                    "low": unscale_price(row["low_scaled"]),
                    "close": unscale_price(row["close_scaled"]),
                    "volume": float(row["volume"]),
                    "trade_count": row["trade_count"],
                    "vwap": unscale_price(row["vwap_scaled"]),
                }
    return result


def resolve_minute_event_gaps(
    symbol: str,
    requests: list[dict],
) -> dict[int, dict]:
    """Resolve sparse/no-trade event minutes within their regular session."""
    instrument = get_instrument(symbol)
    if not instrument or not requests:
        return {}
    normalized = [
        (
            int(item["target_minute"]),
            int(item["open_minute"]),
            int(item["close_minute"]),
        )
        for item in requests
    ]
    result: dict[int, dict] = {}
    with get_intraday_connection() as conn:
        for start in range(0, len(normalized), 200):
            chunk = normalized[start : start + 200]
            values = ",".join("(?, ?, ?)" for _ in chunk)
            params = [value for item in chunk for value in item]
            rows = conn.execute(
                f"""
                WITH requested(target_minute, open_minute, close_minute) AS (
                    VALUES {values}
                )
                SELECT
                    requested.target_minute,
                    (
                        SELECT MAX(previous.minute_utc)
                        FROM minute_bars AS previous
                        WHERE previous.instrument_id = ?
                          AND previous.minute_utc >= requested.open_minute
                          AND previous.minute_utc < requested.target_minute
                    ) AS signal_minute,
                    (
                        SELECT MIN(next.minute_utc)
                        FROM minute_bars AS next
                        WHERE next.instrument_id = ?
                          AND next.minute_utc >= requested.target_minute
                          AND next.minute_utc < requested.close_minute
                    ) AS fill_minute
                FROM requested
                """,
                (*params, int(instrument["id"]), int(instrument["id"])),
            ).fetchall()
            for row in rows:
                result[int(row["target_minute"])] = {
                    "signal_minute": (
                        int(row["signal_minute"])
                        if row["signal_minute"] is not None
                        else None
                    ),
                    "fill_minute": (
                        int(row["fill_minute"])
                        if row["fill_minute"] is not None
                        else None
                    ),
                }
    return result


def get_cumulative_volumes(
    symbol: str,
    requests: list[dict],
) -> dict[str, float]:
    """Aggregate stored volume for many half-open minute ranges."""
    instrument = get_instrument(symbol)
    normalized = [
        (
            str(item["key"]),
            int(item["start_minute"]),
            int(item["end_minute"]),
        )
        for item in requests
        if int(item["end_minute"]) > int(item["start_minute"])
    ]
    if not instrument or not normalized:
        return {}
    result: dict[str, float] = {}
    with get_intraday_connection() as conn:
        for start in range(0, len(normalized), 200):
            chunk = normalized[start : start + 200]
            values = ",".join("(?, ?, ?)" for _ in chunk)
            params = [value for item in chunk for value in item]
            rows = conn.execute(
                f"""
                WITH requested(request_key, start_minute, end_minute) AS (
                    VALUES {values}
                )
                SELECT requested.request_key,
                       COALESCE(SUM(minute_bars.volume), 0) AS volume
                FROM requested
                LEFT JOIN minute_bars
                  ON minute_bars.instrument_id = ?
                 AND minute_bars.minute_utc >= requested.start_minute
                 AND minute_bars.minute_utc < requested.end_minute
                GROUP BY requested.request_key
                """,
                (*params, int(instrument["id"])),
            ).fetchall()
            for row in rows:
                result[str(row["request_key"])] = float(row["volume"] or 0)
    return result


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
                    "volume": float(row["volume"]),
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
        raw_volume = float(row["volume"])
        encoded_volume = (
            int(raw_volume)
            if raw_volume.is_integer()
            else int(round(raw_volume * 1_000_000))
        )
        values = [
            int(row["minute_utc"]),
            int(row["open_scaled"]),
            int(row["high_scaled"]),
            int(row["low_scaled"]),
            int(row["close_scaled"]),
            encoded_volume,
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
