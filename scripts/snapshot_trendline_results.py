from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.db import get_connection  # noqa: E402
from database.repository import get_daily_prices  # noqa: E402
from services.trendline_analysis_service import (  # noqa: E402
    ANALYSIS_CACHE_VERSION,
    _window_fingerprint,
    candles_to_dataframe,
    search_trend_hierarchy,
    serialize_hierarchy,
)


def _daily_rows(symbol: str, limit: int) -> list[dict]:
    rows = [
        row
        for row in get_daily_prices(symbol)
        if date.fromisoformat(row["date"]).weekday() < 5
    ][-limit:]
    return rows


def _fingerprint_rows(rows: list[dict]) -> str:
    candles = [
        {
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in rows
    ]
    return _window_fingerprint(candles)


def _snapshot_symbol(args: tuple[str, int]) -> dict | None:
    symbol, limit = args
    rows = _daily_rows(symbol, limit)
    if len(rows) < 7:
        return None
    hierarchy = search_trend_hierarchy(candles_to_dataframe(rows))
    trends = serialize_hierarchy(hierarchy, 0, len(rows) - 1)
    for trend in trends:
        trend["start_date"] = rows[trend["start_index"]]["date"]
        trend["formation_end_date"] = rows[
            trend["formation_end_index"]
        ]["date"]
        trend["end_date"] = rows[trend["end_index"]]["date"]
        trend["projection_end_date"] = rows[
            trend["projection_end_index"]
        ]["date"]
        trend["break_date"] = (
            None
            if trend["break_index"] is None
            else rows[trend["break_index"]]["date"]
        )
        trend["acceleration_date"] = (
            None
            if trend["acceleration_index"] is None
            else rows[trend["acceleration_index"]]["date"]
        )
        trend["termination_date"] = (
            None
            if trend["termination_index"] is None
            else rows[trend["termination_index"]]["date"]
        )
    return {
        "symbol": symbol,
        "window_start": rows[0]["date"],
        "window_end": rows[-1]["date"],
        "window_size": len(rows),
        "fingerprint": _fingerprint_rows(rows),
        "trends": trends,
    }


def snapshot(limit: int) -> dict:
    with get_connection() as connection:
        symbols = [
            row["symbol"]
            for row in connection.execute(
                "SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol"
            ).fetchall()
        ]

    with ProcessPoolExecutor(max_workers=min(4, len(symbols))) as executor:
        raw_results = executor.map(
            _snapshot_symbol,
            ((symbol, limit) for symbol in symbols),
        )
        results = [item for item in raw_results if item is not None]

    return {
        "created_at": datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat(),
        "algorithm_version": ANALYSIS_CACHE_VERSION,
        "limit": limit,
        "symbols": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        type=Path,
        help="Destination JSON file.",
    )
    parser.add_argument("--limit", type=int, default=150)
    args = parser.parse_args()

    payload = snapshot(args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    line_count = sum(
        len(item["trends"])
        for item in payload["symbols"]
    )
    print(
        f"Saved {len(payload['symbols'])} symbols and "
        f"{line_count} trends to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
