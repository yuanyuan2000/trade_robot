from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from config import FULL_HISTORY_START_DATE, KNOWN_MINUTE_HISTORY_STARTS
from database.intraday_db import intraday_database_info, intraday_quick_check
from database.intraday_repository import (
    get_storage_quality_summary,
    iter_minute_bars,
)


VALIDATION_SYMBOLS = ["GLD", "SPY", "NVDA", "MU", "XLE"]
NEW_YORK = ZoneInfo("America/New_York")


def regular_session_quality(symbol: str) -> dict:
    counts: dict[str, int] = {}
    longest_gap = 0
    previous_date = None
    previous_minute = None
    for row in iter_minute_bars(symbol):
        utc_dt = datetime.fromtimestamp(row["minute_utc"] * 60, tz=timezone.utc)
        local_dt = utc_dt.astimezone(NEW_YORK)
        minute_of_day = local_dt.hour * 60 + local_dt.minute
        if not 570 <= minute_of_day < 960:
            continue
        session_date = local_dt.date().isoformat()
        counts[session_date] = counts.get(session_date, 0) + 1
        if previous_date == session_date and previous_minute is not None:
            longest_gap = max(longest_gap, row["minute_utc"] - previous_minute)
        previous_date = session_date
        previous_minute = row["minute_utc"]
    values = list(counts.values())
    return {
        "regular_sessions": len(values),
        "regular_bars": sum(values),
        "median_bars_per_session": median(values) if values else None,
        "minimum_bars_per_session": min(values) if values else None,
        "sessions_below_95_percent": sum(value < 371 for value in values),
        "longest_in_session_gap_minutes": longest_gap if values else None,
    }


def validate_intraday_storage(symbols: list[str] | None = None) -> dict:
    selected = [symbol.strip().upper() for symbol in (symbols or VALIDATION_SYMBOLS)]
    items = []
    for symbol in selected:
        item = get_storage_quality_summary(symbol)
        if item["row_count"]:
            item.update(regular_session_quality(symbol))
        first_at = item.get("first_at")
        last_at = item.get("last_at")
        expected_start = KNOWN_MINUTE_HISTORY_STARTS.get(
            symbol, {}
        ).get("date", FULL_HISTORY_START_DATE)
        start_grace = (
            datetime.fromisoformat(expected_start).date()
            + timedelta(days=7)
        ).isoformat()
        item["expected_history_start"] = expected_start
        item["coverage_start_ok"] = bool(
            first_at and first_at[:10] <= start_grace
        )
        item["coverage_end_lag_days"] = (
            (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            ).days
            if last_at
            else None
        )
        item["coverage_end_ok"] = (
            item["coverage_end_lag_days"] is not None
            and item["coverage_end_lag_days"] <= 7
        )
        items.append(item)
    database = intraday_database_info()
    total_rows = sum(item["row_count"] for item in items)
    invalid_rows = sum(item["invalid_ohlc_rows"] for item in items)
    negative_rows = sum(item["negative_value_rows"] for item in items)
    coverage_ok = all(
        item["coverage_start_ok"] and item["coverage_end_ok"]
        for item in items
    )
    quick_check = intraday_quick_check()
    return {
        "ok": (
            invalid_rows == 0
            and negative_rows == 0
            and coverage_ok
            and quick_check == "ok"
        ),
        "history_start": FULL_HISTORY_START_DATE,
        "database": {
            **database,
            "quick_check": quick_check,
            "bytes_per_stored_row": (
                round(database["size_bytes"] / database["bar_count"], 2)
                if database["bar_count"]
                else None
            ),
        },
        "symbols": items,
        "totals": {
            "row_count": total_rows,
            "invalid_ohlc_rows": invalid_rows,
            "negative_value_rows": negative_rows,
            "coverage_ok": coverage_ok,
        },
    }
