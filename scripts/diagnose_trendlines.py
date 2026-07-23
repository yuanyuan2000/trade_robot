from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
import sys

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.repository import get_daily_prices  # noqa: E402
from services.trendline_analysis_service import (  # noqa: E402
    candles_to_dataframe,
    search_trend_hierarchy,
    true_range,
)


DEFAULT_SYMBOLS = (
    "XAU/USD",
    "USDINDEX",
    "US10Y",
    "SOXX",
    "MU",
    "SNDK",
    "DRAM",
    "INTC",
    "GDX",
)


def longest_run(mask: np.ndarray) -> int:
    best = current = 0
    for value in mask:
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def diagnose_symbol(symbol: str) -> str:
    rows = [
        row for row in get_daily_prices(symbol)
        if date.fromisoformat(row["date"]).weekday() < 5
    ][-150:]
    df = candles_to_dataframe(rows)
    hierarchy = search_trend_hierarchy(df)
    items = [
        item
        for tier in ("long", "medium", "short")
        for item in hierarchy[tier]
    ]
    output = [f"\n{symbol} ({len(rows)} candles, {len(items)} lines)"]
    atr = true_range(df)
    for item in items:
        trend = item.trend
        indices = np.arange(trend.start, trend.end + 1)
        segment = df.iloc[trend.start:trend.end + 1]
        direction = 1 if trend.direction == "up" else -1
        if trend.direction == "up":
            body_edge = np.minimum(segment["Open"], segment["Close"]).to_numpy()
        else:
            body_edge = np.maximum(segment["Open"], segment["Close"]).to_numpy()
        body_gap = direction * (
            body_edge - trend.y(indices)
        ) / np.maximum(atr[indices], 1e-9)
        crossed = body_gap < -0.10
        severe = body_gap < -0.35
        output.append(
            "  "
            f"{item.id:17} "
            f"{rows[trend.first_touch]['date']}..{rows[trend.last_touch]['date']} "
            f"score={item.tier_score:5.1f} age={item.age:3} "
            f"body_cross={np.mean(crossed):.0%} severe={np.mean(severe):.0%} "
            f"max_run={longest_run(crossed)}"
        )

    for first_index, first in enumerate(items):
        for second in items[first_index + 1:]:
            a, b = first.trend, second.trend
            if a.direction != b.direction:
                continue
            start = max(a.first_touch, b.first_touch)
            end = min(a.last_touch, b.last_touch)
            overlap = end - start + 1
            if overlap <= 1:
                continue
            overlap_ratio = overlap / min(
                a.structure_length,
                b.structure_length,
            )
            positions = np.rint(
                np.linspace(start, end, min(7, overlap))
            ).astype(int)
            distances = np.abs(a.y(positions) - b.y(positions)) / np.maximum(
                atr[positions], 1e-9
            )
            slope_divergence = (
                abs(a.slope - b.slope) * max(1, overlap - 1) /
                max(float(np.median(atr[positions])), 1e-9)
            )
            if overlap_ratio >= 0.40 and float(np.median(distances)) <= 1.20:
                output.append(
                    "    pair "
                    f"{first.id} <> {second.id}: overlap={overlap_ratio:.0%}, "
                    f"median={np.median(distances):.2f} ATR, "
                    f"max={np.max(distances):.2f} ATR, "
                    f"slope_div={slope_divergence:.2f} ATR"
                )
    return "\n".join(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS)
    args = parser.parse_args()
    with ProcessPoolExecutor(max_workers=min(4, len(args.symbols))) as executor:
        for report in executor.map(diagnose_symbol, args.symbols):
            print(report)


if __name__ == "__main__":
    main()
