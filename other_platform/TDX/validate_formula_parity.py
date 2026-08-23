"""Validate the algebra used by the TongdaXin WTMES formula.

This does not emulate the TongdaXin parser.  It verifies that the finite DMA
identity used in ``WTMES.txt`` produces the same latest point-in-time WTME and
rapid-drop result as the project's production Python implementation.
"""

from __future__ import annotations

from pathlib import Path
import math
import random
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.indicator_service import calculate_wtme_components


def _dma(values: list[float], alpha: float) -> list[float]:
    result: list[float] = []
    for value in values:
        result.append(
            value if not result else alpha * value + (1 - alpha) * result[-1]
        )
    return result


def _tdx_latest_components(
    rows: list[dict],
    period: int,
    half_life: float,
    epsilon: float,
) -> dict:
    returns = [0.0]
    normalized_true_ranges = [0.0]
    for index, (previous, current) in enumerate(zip(rows, rows[1:]), start=1):
        previous_close = float(previous["close"])
        current_close = float(current["close"])
        if index == len(rows) - 1:
            # ISLASTBAR branch in WTMES: previous close -> current price only.
            true_range = abs(current_close - previous_close)
        else:
            true_range = max(
                float(current["high"]) - float(current["low"]),
                abs(float(current["high"]) - previous_close),
                abs(float(current["low"]) - previous_close),
            )
        returns.append(current_close / previous_close - 1)
        normalized_true_ranges.append(true_range / previous_close)

    decay = 2 ** (-1 / half_life)
    alpha = 1 - decay
    tail = decay**period
    dma_returns = _dma(returns, alpha)
    dma_ranges = _dma(normalized_true_ranges, alpha)
    current_index = len(rows) - 1
    tail_index = current_index - period
    weighted_return = (
        dma_returns[current_index] - tail * dma_returns[tail_index]
    ) / (1 - tail)
    weighted_true_range = (
        dma_ranges[current_index] - tail * dma_ranges[tail_index]
    ) / (1 - tail)
    return {
        "value": 100 * weighted_return / (weighted_true_range + epsilon),
        "weighted_return": weighted_return,
        "weighted_true_range": weighted_true_range,
    }


def _project_latest_components(
    completed_rows: list[dict],
    current_price: float,
    period: int,
    half_life: float,
    epsilon: float,
) -> dict:
    previous_close = float(completed_rows[-1]["close"])
    current = {
        "date": "current",
        "open": previous_close,
        "high": max(previous_close, current_price),
        "low": min(previous_close, current_price),
        "close": current_price,
    }
    result = calculate_wtme_components(
        [*completed_rows[-period:], current],
        period,
        half_life,
        epsilon,
    )
    if result is None:
        raise AssertionError("project WTME unexpectedly returned None")
    return result


def _rapid_drop(
    completed_rows: list[dict],
    current_price: float,
    lookback: int,
    threshold_percent: float,
) -> bool:
    previous_rows = completed_rows[-lookback:]
    current_prices = [
        *[float(row["close"]) for row in previous_rows[1:]],
        current_price,
    ]
    return any(
        current_value / float(previous["close"]) - 1
        <= -threshold_percent / 100
        for previous, current_value in zip(previous_rows, current_prices)
    )


def _make_rows(seed: int, count: int) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    close = 100.0
    for index in range(count):
        previous_close = close
        close = previous_close * (1 + rng.uniform(-0.035, 0.035))
        high = max(previous_close, close) * (1 + rng.uniform(0.0, 0.025))
        low = min(previous_close, close) * (1 - rng.uniform(0.0, 0.025))
        rows.append(
            {
                "date": f"D{index:03d}",
                "open": previous_close,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return rows


def main() -> int:
    epsilon = 1e-8
    checked = 0
    for seed in range(20):
        completed = _make_rows(seed, 90)
        previous_close = float(completed[-1]["close"])
        for period, half_life in ((13, 6.0), (40, 15.0)):
            for current_move in (-0.061, -0.02, 0.0, 0.017, 0.055):
                current_price = previous_close * (1 + current_move)
                project = _project_latest_components(
                    completed,
                    current_price,
                    period,
                    half_life,
                    epsilon,
                )
                current_row = {
                    "date": "current",
                    "open": previous_close,
                    "high": previous_close,
                    "low": previous_close,
                    "close": current_price,
                }
                tdx = _tdx_latest_components(
                    [*completed, current_row],
                    period,
                    half_life,
                    epsilon,
                )
                for key in ("value", "weighted_return", "weighted_true_range"):
                    if not math.isclose(
                        float(project[key]),
                        float(tdx[key]),
                        rel_tol=1e-11,
                        abs_tol=1e-12,
                    ):
                        raise AssertionError(
                            f"seed={seed} N={period} h={half_life} "
                            f"move={current_move} {key}: "
                            f"project={project[key]} tdx={tdx[key]}"
                        )

                tdx_drop = any(
                    float(current["close"]) / float(previous["close"]) - 1
                    <= -0.05
                    for previous, current in zip(
                        [*completed, current_row][-6:-1],
                        [*completed, current_row][-5:],
                    )
                )
                project_drop = _rapid_drop(completed, current_price, 5, 5.0)
                if tdx_drop != project_drop:
                    raise AssertionError(
                        f"rapid-drop mismatch: seed={seed}, move={current_move}"
                    )
                checked += 1

    print(f"WTMES parity OK: {checked} point-in-time cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
