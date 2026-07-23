from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.db import get_connection  # noqa: E402
from database.repository import get_daily_prices  # noqa: E402
from services.trendline_analysis_service import (  # noqa: E402
    _lines_are_duplicates,
    candles_to_dataframe,
    search_trend_hierarchy,
)


REAL_CASES = (
    ("USDINDEX", "up", "2026-01-27", "2026-04-27"),
    ("SPX", "down", "2026-03-02", "2026-03-31"),
    ("US10Y", "up", "2026-02-27", "2026-03-31"),
    ("MU", "up", "2025-12-17", "2026-02-06"),
    ("GDX", "down", "2026-05-11", "2026-07-13"),
)


def synthetic_ohlc(kind: str, n: int = 150, seed: int = 19) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    baseline = 100 + 0.11 * t
    if kind == "distributed_up":
        close = baseline + 0.3 + 3.0 * np.abs(np.sin(np.pi * t / 13))
    elif kind == "endpoint_bridge":
        close = baseline + 0.3 + 7.0 * np.sin(np.pi * t / (n - 1)) ** 2
    elif kind == "regime_switch":
        close = 105 + 0.18 * t + 2.0 * np.sin(t / 6)
        pivot = 112
        close[pivot:] = close[pivot - 1] - 0.42 * np.arange(1, n - pivot + 1)
    elif kind == "sideways":
        close = 110 + np.cumsum(rng.normal(0, 0.45, n))
        close = 110 + 0.8 * (
            close - pd.Series(close).rolling(18, min_periods=1).mean()
        )
    else:
        raise ValueError(kind)

    close = np.asarray(close) + rng.normal(0, 0.10, n)
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.09, n)
    high = np.maximum(open_, close) + rng.uniform(0.22, 0.42, n)
    low = np.minimum(open_, close) - rng.uniform(0.22, 0.42, n)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": np.full(n, 1_000_000),
        }
    )


def flatten(hierarchy: dict) -> list:
    return [
        item
        for tier in ("long", "medium", "short")
        for item in hierarchy[tier]
    ]


def validate_synthetic() -> list[tuple[str, bool, str]]:
    results = []
    for kind in ("distributed_up", "endpoint_bridge", "regime_switch", "sideways"):
        df = synthetic_ohlc(kind)
        items = flatten(search_trend_hierarchy(df))
        duplicate_pairs = sum(
            _lines_are_duplicates(df, first.trend, second.trend)
            for index, first in enumerate(items)
            for second in items[index + 1:]
        )
        if kind == "distributed_up":
            passed = any(
                item.trend.direction == "up" and
                item.trend.structure_length >= 50
                for item in items
            )
        elif kind == "endpoint_bridge":
            passed = not any(
                item.tier == "long" and item.trend.structure_length >= 120
                for item in items
            )
        elif kind == "regime_switch":
            passed = (
                any(item.trend.direction == "up" for item in items) and
                any(item.trend.direction == "down" for item in items)
            )
        else:
            passed = len(items) <= 3
        passed = passed and duplicate_pairs == 0
        results.append(
            (f"synthetic:{kind}", passed,
             f"lines={len(items)}, duplicate_pairs={duplicate_pairs}")
        )
    return results


def nearest_index(rows: list[dict], target: str) -> int:
    target_date = date.fromisoformat(target)
    return min(
        range(len(rows)),
        key=lambda index: abs(
            (date.fromisoformat(rows[index]["date"]) - target_date).days
        ),
    )


def validate_real() -> list[tuple[str, bool, str]]:
    results = []
    for symbol, direction, start_date, end_date in REAL_CASES:
        rows = [
            row for row in get_daily_prices(symbol)
            if date.fromisoformat(row["date"]).weekday() < 5
        ][-150:]
        if len(rows) < 50:
            results.append((f"real:{symbol}", False, "insufficient data"))
            continue
        target_start = nearest_index(rows, start_date)
        target_end = nearest_index(rows, end_date)
        items = flatten(search_trend_hierarchy(candles_to_dataframe(rows)))
        matches = []
        for item in items:
            trend = item.trend
            if trend.direction != direction:
                continue
            display_end = len(rows) - 1 if item.active else trend.last_touch
            intersection = max(
                0,
                min(display_end, target_end) -
                max(trend.first_touch, target_start) + 1,
            )
            target_length = target_end - target_start + 1
            coverage = intersection / max(1, target_length)
            display_length = display_end - trend.first_touch + 1
            union = display_length + target_length - intersection
            interval_iou = intersection / max(1, union)
            boundary_error = (
                abs(trend.first_touch - target_start) +
                abs(display_end - target_end)
            )
            matches.append(
                (interval_iou, coverage, -boundary_error, item, display_end)
            )
        best = max(matches, default=None, key=lambda value: value[:2])
        passed = bool(best and best[0] >= 0.35 and best[1] >= 0.60)
        if best:
            trend = best[3].trend
            detail = (
                f"coverage={best[1]:.0%}, iou={best[0]:.0%}, "
                f"detected={rows[trend.first_touch]['date']}.."
                f"{rows[best[4]]['date']}, "
                f"tier={best[3].tier}, score={best[3].tier_score:.1f}"
            )
        else:
            detail = "no same-direction line"
        results.append((f"real:{symbol}", passed, detail))
    return results


def audit_symbol(symbol: str) -> tuple[str, bool, str]:
    rows = [
        row for row in get_daily_prices(symbol)
        if date.fromisoformat(row["date"]).weekday() < 5
    ][-150:]
    if len(rows) < 30:
        return f"audit:{symbol}", True, f"skipped, candles={len(rows)}"

    df = candles_to_dataframe(rows)
    items = flatten(search_trend_hierarchy(df))
    duplicate_pairs = sum(
        _lines_are_duplicates(df, first.trend, second.trend)
        for index, first in enumerate(items)
        for second in items[index + 1:]
    )
    valid_geometry = all(
        0 <= item.trend.first_touch <= item.trend.last_touch < len(rows) and
        np.isfinite(item.trend.slope) and
        np.isfinite(item.trend.intercept)
        for item in items
    )
    valid_long_gaps = all(
        item.tier != "long" or item.trend.max_touch_gap <= 0.80
        for item in items
    )
    valid_body_crossings = all(
        item.trend.max_body_breach_run <= 2
        for item in items
    )
    passed = (
        duplicate_pairs == 0 and
        valid_geometry and
        valid_long_gaps and
        valid_body_crossings and
        len(items) <= 6
    )
    detail = (
        f"candles={len(rows)}, lines={len(items)}, "
        f"duplicate_pairs={duplicate_pairs}"
    )
    return f"audit:{symbol}", passed, detail


def validate_all_symbols() -> list[tuple[str, bool, str]]:
    with get_connection() as connection:
        symbols = [
            row["symbol"]
            for row in connection.execute(
                "SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol"
            ).fetchall()
        ]
    with ProcessPoolExecutor(max_workers=min(4, len(symbols))) as executor:
        return list(executor.map(audit_symbol, symbols))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--synthetic-only",
        action="store_true",
        help="Skip the slower database-backed market cases.",
    )
    parser.add_argument(
        "--all-symbols",
        action="store_true",
        help="Also audit every symbol currently stored in SQLite.",
    )
    args = parser.parse_args()

    results = validate_synthetic()
    if not args.synthetic_only:
        results.extend(validate_real())
    if args.all_symbols:
        results.extend(validate_all_symbols())
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL':4} {name:28} {detail}")
    failures = [name for name, passed, _ in results if not passed]
    if failures:
        print(f"\nFailed: {', '.join(failures)}")
        return 1
    print(f"\nAll {len(results)} validation cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
