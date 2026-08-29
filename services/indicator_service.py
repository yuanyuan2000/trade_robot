from __future__ import annotations

import math
from typing import Iterable

import numpy as np


WTME_DEFAULT_HALF_LIFE = 15.0
WTME_DEFAULT_EPSILON = 1e-8
INDICATOR_CONTRACT_VERSION = 1
R_SQUARE_TOLERANCE = 1e-12


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


def calculate_r_square(
    rows: list[dict],
    period: int = 25,
) -> list[float | None]:
    """Return the SevenStar-style weighted log-price R-squared series.

    ``period`` is the number of completed intervals before the current point,
    so every observation uses ``period + 1`` closes.  The final row is treated
    exactly like any other row; when it is an unfinished K-line its current
    ``close`` therefore becomes the point-in-time final price.
    """
    values: list[float | None] = [None] * len(rows)
    if period < 2 or len(rows) < period + 1:
        return values

    point_count = period + 1
    x = np.arange(point_count, dtype=float)
    fit_weights = np.linspace(1.0, 2.0, point_count)
    importance = fit_weights ** 2
    importance_total = float(np.sum(importance))
    weighted_x_mean = float(np.average(x, weights=importance))
    centered_x = x - weighted_x_mean
    weighted_x_variance = float(np.sum(importance * centered_x ** 2))

    closes = np.full(len(rows), np.nan, dtype=float)
    for index, row in enumerate(rows):
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            closes[index] = close

    log_prices = np.full(len(rows), np.nan, dtype=float)
    valid_prices = np.isfinite(closes)
    log_prices[valid_prices] = np.log(closes[valid_prices])
    windows = np.lib.stride_tricks.sliding_window_view(log_prices, point_count)
    valid_windows = np.all(np.isfinite(windows), axis=1)
    if not np.any(valid_windows):
        return values

    output_indexes = np.flatnonzero(valid_windows) + period
    y = windows[valid_windows]
    weighted_means = np.sum(y * importance, axis=1) / importance_total
    centered_y = y - weighted_means[:, None]
    ss_tot = np.sum(importance * centered_y ** 2, axis=1)
    weighted_variances = ss_tot / importance_total
    finite_variances = np.isfinite(weighted_variances)

    flat = finite_variances & (weighted_variances <= np.finfo(float).eps)
    for output_index in output_indexes[flat]:
        values[int(output_index)] = 0.0

    fitted = finite_variances & ~flat
    if not np.any(fitted):
        return values
    fitted_indexes = output_indexes[fitted]
    fitted_y = centered_y[fitted]
    slopes = (
        np.sum(importance * centered_x * fitted_y, axis=1)
        / weighted_x_variance
    )
    fitted_centered = slopes[:, None] * centered_x
    ss_res = np.sum(importance * (fitted_y - fitted_centered) ** 2, axis=1)
    raw_r_squared = 1 - ss_res / ss_tot[fitted]
    acceptable = (
        np.isfinite(raw_r_squared)
        & (raw_r_squared >= -R_SQUARE_TOLERANCE)
        & (raw_r_squared <= 1 + R_SQUARE_TOLERANCE)
    )
    result = np.zeros(len(raw_r_squared), dtype=float)
    result[acceptable] = np.clip(raw_r_squared[acceptable], 0.0, 1.0)
    for output_index, value in zip(fitted_indexes, result):
        values[int(output_index)] = float(value)
    return values


def calculate_indicator_values(
    rows: list[dict],
    indicator_type: str,
    period: int | None,
    *,
    half_life: float | None = None,
    epsilon: float = WTME_DEFAULT_EPSILON,
    threshold_percent: float | None = None,
    fast_period: int | None = None,
    slow_period: int | None = None,
    signal_period: int | None = None,
) -> list[float | None]:
    normalized_type = str(indicator_type).strip().upper()
    if normalized_type == "MACD":
        return calculate_macd_components(
            rows,
            int(fast_period or 12),
            int(slow_period or 26),
            int(signal_period or 9),
        )["histogram"]
    if period is None:
        return [None] * len(rows)
    minimum_period = 2 if normalized_type in {"WTME", "LINEAR_FIT"} else 1
    if period < minimum_period:
        return [None] * len(rows)
    if normalized_type == "MA":
        return _calculate_ma(rows, period)
    if normalized_type == "EMA":
        return _calculate_ema(rows, period)
    if normalized_type == "RSI":
        return calculate_wilder_rsi(rows, period)
    if normalized_type == "ATR":
        return calculate_wilder_atr(rows, period)
    if normalized_type == "RATR":
        return _calculate_relative_atr_score(rows, period)
    if normalized_type == "LINEAR_FIT":
        return calculate_r_square(rows, period)
    if normalized_type == "WTME":
        return calculate_wtme(
            rows,
            period,
            WTME_DEFAULT_HALF_LIFE if half_life is None else half_life,
            epsilon,
        )
    if normalized_type == "RAPID_DROP":
        return calculate_rapid_drop_filter(
            rows,
            period,
            float(threshold_percent) if threshold_percent is not None else 5.0,
        )
    return [None] * len(rows)


def calculate_wilder_rsi(rows: list[dict], period: int = 14) -> list[float | None]:
    """Return Wilder RSI, seeded from the first ``period`` close changes."""
    values: list[float | None] = [None] * len(rows)
    if period < 1 or len(rows) <= period:
        return values
    changes = [
        float(current["close"]) - float(previous["close"])
        for previous, current in zip(rows, rows[1:])
    ]
    average_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    average_loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    values[period] = rsi(average_gain, average_loss)
    for row_index, change in enumerate(changes[period:], start=period + 1):
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        values[row_index] = rsi(average_gain, average_loss)
    return values


def calculate_macd_components(
    rows: list[dict],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict[str, list[float | None]]:
    """Return MACD line (DIF), signal (DEA), and undoubled histogram."""
    empty = [None] * len(rows)
    if min(fast_period, slow_period, signal_period) < 1 or fast_period >= slow_period:
        return {"line": empty[:], "signal": empty[:], "histogram": empty[:]}
    fast = _calculate_ema(rows, fast_period)
    slow = _calculate_ema(rows, slow_period)
    line: list[float | None] = [
        float(fast_value) - float(slow_value)
        if fast_value is not None and slow_value is not None else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    signal: list[float | None] = [None] * len(rows)
    valid = [(index, value) for index, value in enumerate(line) if value is not None]
    if len(valid) >= signal_period:
        seed = sum(float(value) for _, value in valid[:signal_period]) / signal_period
        seed_index = valid[signal_period - 1][0]
        signal[seed_index] = seed
        multiplier = 2 / (signal_period + 1)
        previous = seed
        for index, value in valid[signal_period:]:
            previous = float(value) * multiplier + previous * (1 - multiplier)
            signal[index] = previous
    histogram = [
        float(line_value) - float(signal_value)
        if line_value is not None and signal_value is not None else None
        for line_value, signal_value in zip(line, signal)
    ]
    return {"line": line, "signal": signal, "histogram": histogram}


def _indicator_calculation(indicator: dict, rows: list[dict]) -> tuple[list[float | None], dict[str, list[float | None]]]:
    params = indicator.get("params") or {}
    indicator_type = str(indicator.get("indicator_type", "")).upper()
    if indicator_type == "MACD":
        components = calculate_macd_components(
            rows,
            int(params.get("fast_period", 12)),
            int(params.get("slow_period", 26)),
            int(params.get("signal_period", 9)),
        )
        return components["histogram"], components
    values = calculate_indicator_values(
        rows,
        indicator_type,
        int(params.get("period")),
        half_life=params.get("half_life"),
        epsilon=float(params.get("epsilon", WTME_DEFAULT_EPSILON)),
        threshold_percent=params.get("threshold_percent"),
    )
    return values, {}


def latest_indicator_value(
    rows: list[dict],
    indicator: dict,
    *,
    price_basis: str | None = None,
    as_of: str | None = None,
) -> dict:
    values, components = _indicator_calculation(indicator, rows)
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        if value is not None:
            row = rows[index]
            result = {
                "value": float(value),
                "date": row.get("date"),
                "as_of": as_of or row.get("updated_at") or row.get("date"),
                "is_provisional": not bool(row.get("is_complete", True)),
                "price_basis": price_basis or row.get("price_basis") or "raw",
                "indicator_contract_version": INDICATOR_CONTRACT_VERSION,
            }
            if components:
                result["components"] = {
                    name: float(series[index]) if series[index] is not None else None
                    for name, series in components.items()
                }
            return result
    latest = rows[-1] if rows else {}
    return {
        "value": None,
        "date": latest.get("date"),
        "as_of": as_of or latest.get("updated_at") or latest.get("date"),
        "is_provisional": not bool(latest.get("is_complete", True)) if rows else False,
        "price_basis": price_basis or latest.get("price_basis") or "raw",
        "indicator_contract_version": INDICATOR_CONTRACT_VERSION,
    }


def attach_overview_indicator_values(
    overview: dict,
    indicators: Iterable[dict],
    daily_rows_by_symbol: dict[str, list[dict]],
    calculation_metadata_by_symbol: dict[str, dict] | None = None,
) -> dict:
    selected = list(indicators)
    calculation_metadata_by_symbol = calculation_metadata_by_symbol or {}
    for item in overview.get("items", []):
        rows = daily_rows_by_symbol.get(item["symbol"], [])
        metadata = calculation_metadata_by_symbol.get(item["symbol"], {})
        item["indicator_values"] = {
            str(indicator["id"]): latest_indicator_value(
                rows,
                indicator,
                price_basis=metadata.get("price_basis"),
                as_of=metadata.get("as_of"),
            )
            for indicator in selected
        }
    overview["selected_indicators"] = selected
    return overview


def build_indicator_series(
    rows: list[dict],
    indicators: Iterable[dict],
    *,
    price_basis: str,
    as_of: str | None = None,
) -> list[dict]:
    """Calculate chart-ready indicator points with the canonical backend.

    Points carry bar keys rather than relying on array position so the browser
    can safely align them after a concurrent refresh or a weekend filter.
    """
    result: list[dict] = []
    for indicator in indicators:
        values, components = _indicator_calculation(indicator, rows)
        points = []
        for row, value in zip(rows, values):
            point = {
                "date": row.get("date"),
                "endDate": row.get("endDate"),
                "value": float(value) if value is not None else None,
                "is_provisional": not bool(row.get("is_complete", True)),
                "as_of": row.get("updated_at") or as_of or row.get("date"),
            }
            if components:
                point["components"] = {
                    name: float(series[len(points)]) if series[len(points)] is not None else None
                    for name, series in components.items()
                }
            points.append(point)
        latest = points[-1] if points else {}
        result.append({
            **indicator,
            "points": points,
            "price_basis": price_basis,
            "as_of": latest.get("as_of") or as_of,
            "is_provisional": bool(latest.get("is_provisional", False)),
            "indicator_contract_version": INDICATOR_CONTRACT_VERSION,
        })
    return result


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


def calculate_rapid_drop_filter(
    rows: list[dict],
    period: int,
    threshold_percent: float,
) -> list[float | None]:
    """Flag whether any of the latest N close-to-close moves is a rapid drop.

    The value ending at row ``i`` examines exactly ``period`` consecutive
    changes, including the change into row ``i``.  Callers deliberately pass
    through an unfinished latest daily bar, so its current close participates
    in the newest change just like the live event price in the rapid-drop
    rotation strategies.
    """
    values: list[float | None] = [None] * len(rows)
    if period < 1 or threshold_percent <= 0:
        return values
    threshold = -float(threshold_percent) / 100
    for index in range(period, len(rows)):
        triggered = False
        valid = True
        for current_index in range(index - period + 1, index + 1):
            previous_close = float(rows[current_index - 1]["close"])
            if previous_close <= 0:
                valid = False
                break
            current_close = float(rows[current_index]["close"])
            if current_close / previous_close - 1 <= threshold:
                triggered = True
        if valid:
            values[index] = 1.0 if triggered else 0.0
    return values


def calculate_wtme_components(
    rows: list[dict],
    period: int,
    half_life: float,
    epsilon: float = WTME_DEFAULT_EPSILON,
) -> dict | None:
    """Return WTME and its weighted numerator/denominator for the latest bar.

    ``period`` represents N true-range/return observations and therefore needs
    N+1 OHLC bars.  The newest observation has raw weight 1 and an observation
    ``half_life`` sessions older has half that weight, exactly matching the
    definition in ``new_indicator.md``.
    """
    if period < 2 or half_life <= 0 or epsilon <= 0 or len(rows) < period + 1:
        return None

    window = rows[-(period + 1):]
    raw_weights = [
        2 ** (-(period - 1 - index) / half_life)
        for index in range(period)
    ]
    weight_total = sum(raw_weights)
    weights = [value / weight_total for value in raw_weights]
    returns: list[float] = []
    normalized_true_ranges: list[float] = []
    for previous, current in zip(window, window[1:]):
        previous_close = float(previous["close"])
        if previous_close <= 0:
            return None
        current_close = float(current["close"])
        high = float(current["high"])
        low = float(current["low"])
        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        returns.append((current_close - previous_close) / previous_close)
        normalized_true_ranges.append(true_range / previous_close)

    weighted_return = sum(
        weight * value for weight, value in zip(weights, returns)
    )
    weighted_true_range = sum(
        weight * value
        for weight, value in zip(weights, normalized_true_ranges)
    )
    return {
        "value": 100 * weighted_return / (weighted_true_range + epsilon),
        "weighted_return": weighted_return,
        "weighted_true_range": weighted_true_range,
        "weights": weights,
        "returns": returns,
        "normalized_true_ranges": normalized_true_ranges,
    }


def calculate_wtme(
    rows: list[dict],
    period: int,
    half_life: float,
    epsilon: float = WTME_DEFAULT_EPSILON,
) -> list[float | None]:
    """Calculate the WTME value ending at every completed bar."""
    values: list[float | None] = [None] * len(rows)
    for index in range(period, len(rows)):
        components = calculate_wtme_components(
            rows[index - period:index + 1],
            period,
            half_life,
            epsilon,
        )
        if components is not None:
            values[index] = float(components["value"])
    return values
