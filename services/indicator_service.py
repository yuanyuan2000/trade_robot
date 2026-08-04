from __future__ import annotations

from typing import Iterable


def calculate_wilder_atr(
    rows: list[dict],
    period: int,
) -> list[float | None]:
    """Return Wilder ATR values ending at each completed bar.

    The first true range starts on the second bar because it requires the
    previous close.  The first ATR is the arithmetic mean of the first
    ``period`` true ranges; subsequent values use Wilder's 1/period update.
    This is the same non-lookahead convention used by the shipped rapid-drop
    and ATR-rotation strategy.
    """
    values: list[float | None] = [None] * len(rows)
    true_ranges: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        high = float(current["high"])
        low = float(current["low"])
        previous_close = float(previous["close"])
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )

    if len(true_ranges) < period:
        return values

    atr = sum(true_ranges[:period]) / period
    values[period] = atr
    for row_index, true_range in enumerate(true_ranges[period:], start=period + 1):
        atr = (atr * (period - 1) + true_range) / period
        values[row_index] = atr
    return values


def calculate_indicator_values(
    rows: list[dict],
    indicator_type: str,
    period: int,
) -> list[float | None]:
    normalized_type = str(indicator_type).strip().upper()
    if period < 2:
        return [None] * len(rows)
    if normalized_type == "MA":
        return _calculate_ma(rows, period)
    if normalized_type == "EMA":
        return _calculate_ema(rows, period)
    if normalized_type == "ATR":
        return calculate_wilder_atr(rows, period)
    if normalized_type == "RATR":
        return _calculate_relative_atr_score(rows, period)
    return [None] * len(rows)


def latest_indicator_value(
    rows: list[dict],
    indicator: dict,
) -> dict:
    period = int((indicator.get("params") or {}).get("period"))
    values = calculate_indicator_values(rows, indicator.get("indicator_type", ""), period)
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        if value is not None:
            return {"value": float(value), "date": rows[index].get("date")}
    return {"value": None, "date": rows[-1].get("date") if rows else None}


def attach_overview_indicator_values(
    overview: dict,
    indicators: Iterable[dict],
    daily_rows_by_symbol: dict[str, list[dict]],
) -> dict:
    selected = list(indicators)
    for item in overview.get("items", []):
        rows = daily_rows_by_symbol.get(item["symbol"], [])
        item["indicator_values"] = {
            str(indicator["id"]): latest_indicator_value(rows, indicator)
            for indicator in selected
        }
    overview["selected_indicators"] = selected
    return overview


def _calculate_ma(rows: list[dict], period: int) -> list[float | None]:
    values: list[float | None] = []
    total = 0.0
    for index, row in enumerate(rows):
        total += float(row["close"])
        if index >= period:
            total -= float(rows[index - period]["close"])
        values.append(total / period if index >= period - 1 else None)
    return values


def _calculate_ema(rows: list[dict], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(rows)
    if len(rows) < period:
        return values
    previous = sum(float(row["close"]) for row in rows[:period]) / period
    values[period - 1] = previous
    multiplier = 2 / (period + 1)
    for index in range(period, len(rows)):
        previous = float(rows[index]["close"]) * multiplier + previous * (1 - multiplier)
        values[index] = previous
    return values


def _calculate_relative_atr_score(
    rows: list[dict],
    period: int,
) -> list[float | None]:
    """Calculate the strategy's signed relative-ATR momentum score.

    At bar i the score uses the current close, the close N completed bars ago,
    and the Wilder ATR known before bar i.  Excluding the current bar from ATR
    mirrors a real-time decision made using the current event price and avoids
    leaking its completed high/low into the denominator.
    """
    values: list[float | None] = [None] * len(rows)
    atr_values = calculate_wilder_atr(rows, period)
    for index in range(period + 1, len(rows)):
        atr = atr_values[index - 1]
        if atr is None or atr <= 0:
            continue
        current_close = float(rows[index]["close"])
        base_close = float(rows[index - period]["close"])
        values[index] = (current_close - base_close) / atr
    return values
