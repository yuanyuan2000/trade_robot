from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from typing import Iterable
from zoneinfo import ZoneInfo

from database import intraday_repository, repository
from services.backtest.corporate_actions import (
    ensure_corporate_actions,
    validate_supported_actions,
)
from services.backtest.errors import BacktestDataError, BacktestValidationError
from services.backtest.market_calendar import ensure_market_sessions
from services.market_context import (
    calendar_contract,
    normalize_market_config,
    strategy_daily_series,
    uses_previous_close_for_historical_intraday,
)
from services.indicator_service import (
    WTME_DEFAULT_EPSILON,
    calculate_macd_components,
    calculate_indicator_values,
)


NEW_YORK = ZoneInfo("America/New_York")


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _epoch_minute(local_date: str, hhmm: str) -> int:
    parsed_date = date.fromisoformat(local_date)
    parsed_time = time.fromisoformat(hhmm)
    local = datetime.combine(parsed_date, parsed_time, tzinfo=NEW_YORK)
    return int(local.astimezone(timezone.utc).timestamp()) // 60


def _minute_label(value: int) -> str:
    return (
        datetime.fromtimestamp(int(value) * 60, tz=timezone.utc)
        .astimezone(NEW_YORK)
        .strftime("%Y-%m-%d %H:%M America/New_York")
    )


def _validate_bar(row: dict, *, symbol: str, granularity: str) -> dict | None:
    try:
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = float(row.get("volume") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "symbol": symbol,
            "type": f"{granularity}_invalid",
            "at": row.get("date") or row.get("timestamp") or row.get("minute_utc"),
            "reason": f"字段缺失或不是数值：{exc}",
        }
    values = (open_price, high, low, close, volume)
    if not all(math.isfinite(value) for value in values):
        reason = "包含 NaN 或 Infinity"
    elif min(open_price, high, low, close) <= 0:
        reason = "价格必须大于 0"
    elif volume < 0:
        reason = "成交量不能为负"
    elif low > min(open_price, close) or high < max(open_price, close) or low > high:
        reason = "OHLC 关系不合法"
    else:
        return None
    return {
        "symbol": symbol,
        "type": f"{granularity}_invalid",
        "at": row.get("date") or row.get("timestamp") or row.get("minute_utc"),
        "reason": reason,
    }


@dataclass(frozen=True)
class EventPrice:
    signal_price: float
    fill_price: float | None
    signal_time: str
    fill_time: str | None


class ExpressionContext:
    def __init__(
        self,
        dataset: "HistoricalDataSet",
        *,
        symbol: str,
        trading_date: str,
        event: str,
        price: float,
        position: float,
    ):
        self.dataset = dataset
        self.symbol = symbol
        self.trading_date = trading_date
        self.event = event
        self.price = float(price)
        self.position = float(position)
        self._function_cache: dict[tuple[str, tuple[float | int, ...]], float] = {}

    def resolve_function(self, name: str, *arguments: float | int) -> float:
        cache_key = (name, tuple(arguments))
        if cache_key in self._function_cache:
            return self._function_cache[cache_key]
        period = int(arguments[0])
        if name in {"open", "high", "low", "close", "volume"}:
            value = self.dataset.value_before(
                self.symbol, self.trading_date, name, period
            )
        elif name in {
            "ma", "ema", "atr", "volat", "rsi", "ratr", "r_square", "wtme",
            "rapid_drop", "macd_line", "macd_signal", "macd_hist",
        }:
            value = self.dataset.indicator_value(
                self.symbol,
                self.trading_date,
                self.event,
                self.price,
                name,
                tuple(arguments),
            )
        else:
            raise BacktestValidationError(f"不支持指标函数 {name}。")
        if value is None:
            rendered = ",".join(f"{float(item):g}" for item in arguments)
            raise BacktestDataError(
                f"{self.symbol} 没有足够数据计算 {name}({rendered})。"
            )
        result = float(value)
        self._function_cache[cache_key] = result
        return result

    def _point_in_time_rows(self) -> list[dict]:
        if self.event == "CLOSE":
            return self.dataset.indicator_history(
                self.symbol,
                self.trading_date,
                include_current=True,
            )
        completed = self.dataset.daily_before(self.symbol, self.trading_date)
        if not completed:
            return []
        previous_close = float(completed[-1]["close"])
        return [
            *completed,
            {
                "date": self.trading_date,
                "open": previous_close,
                "high": max(previous_close, self.price),
                "low": min(previous_close, self.price),
                "close": self.price,
                "volume": 0.0,
                "is_complete": 0,
            },
        ]


class HistoricalDataSet:
    def __init__(
        self,
        *,
        daily: dict[str, list[dict]],
        sessions: list[str],
        minute: dict[str, dict[int, dict]] | None = None,
        intraday_event_minutes: dict[str, dict[str, dict]] | None = None,
        cumulative_volumes: dict[str, dict[str, float]] | None = None,
        availability_start: dict[str, str | None] | None = None,
        required_intraday_events: Iterable[str] = (),
        corporate_actions: list[dict] | None = None,
        manifest: dict | None = None,
    ):
        self.daily = {
            symbol: sorted((dict(row) for row in rows), key=lambda row: row["date"])
            for symbol, rows in daily.items()
        }
        self.daily_maps = {
            symbol: {row["date"]: row for row in rows}
            for symbol, rows in self.daily.items()
        }
        self.daily_dates = {
            symbol: [row["date"] for row in rows]
            for symbol, rows in self.daily.items()
        }
        self.sessions = list(sessions)
        self.minute = minute or {}
        self.intraday_event_minutes = intraday_event_minutes or {}
        self.cumulative_volumes = cumulative_volumes or {}
        self.availability_start = availability_start or {
            symbol: (rows[0]["date"] if rows else None)
            for symbol, rows in self.daily.items()
        }
        self.required_intraday_events = sorted(set(required_intraday_events))
        self.corporate_actions = list(corporate_actions or [])
        self.actions_by_date: dict[str, list[dict]] = {}
        for action in self.corporate_actions:
            effective_date = action.get("ex_date") or action["process_date"]
            self.actions_by_date.setdefault(effective_date, []).append(action)
        self._dividend_factors = self._build_dividend_factors()
        self._build_adjustment_indexes()
        self._indicator_series_cache: dict[tuple, object] = {}
        self._volatility_prefix_cache: dict[str, tuple[list[float], list[float], list[int]]] = {}
        self._volatility_date_index_cache: dict[str, dict[str, int]] = {}
        self._strategy_atr_cache: dict[tuple, list[float | None]] = {}
        self.manifest = manifest or {}
        self._session_index = {value: index for index, value in enumerate(self.sessions)}
        self._market_session_minutes = {
            item["trading_date"]: (
                int(item["open_minute_utc"]),
                int(item["close_minute_utc"]),
            )
            for item in self.manifest.get("market_sessions", [])
        }

    def _build_dividend_factors(self) -> dict[tuple[str, str], float]:
        """Build point-in-time price factors without modifying executable bars.

        Indicators see a continuous history as of each session.  Portfolio
        valuation and fills continue to use raw bars while dividends are
        accounted for separately as receivables/cash.
        """
        totals: dict[tuple[str, str], float] = {}
        for action in self.corporate_actions:
            if (
                action.get("action_type") != "cash_dividend"
                or not action.get("affects_position", True)
            ):
                continue
            symbol = action["symbol"]
            effective_date = action.get("ex_date") or action["process_date"]
            key = (symbol, effective_date)
            totals[key] = totals.get(key, 0.0) + float(action["cash_rate"])

        factors: dict[tuple[str, str], float] = {}
        for (symbol, effective_date), total in totals.items():
            rows = self.daily.get(symbol, [])
            end = bisect_left(self.daily_dates.get(symbol, []), effective_date)
            if end <= 0:
                # No loaded history predates this event, so there is nothing
                # in the indicator window to adjust.
                continue
            previous_close = float(rows[end - 1]["close"])
            if total < 0 or previous_close <= 0 or total >= previous_close:
                raise BacktestDataError(f"{symbol} {effective_date} 分红复权因子无效。")
            factors[(symbol, effective_date)] = (
                previous_close - total
            ) / previous_close
        return factors

    def _build_adjustment_indexes(self) -> None:
        """Index cumulative point-in-time adjustment factors by symbol/date.

        If ``P(t)`` is the cumulative price factor through date ``t``, a raw
        bar dated ``d`` viewed as of ``t`` is ``raw * P(t) / P(d)``.  The same
        ratio rule applies to split-adjusted volume with a separate factor.
        This preserves the existing point-in-time semantics without scanning
        every corporate action for every historical row.
        """
        events: dict[str, dict[str, list[float]]] = {}

        def multipliers(symbol: str, effective_date: str) -> list[float]:
            return events.setdefault(symbol, {}).setdefault(effective_date, [1.0, 1.0])

        for action in self.corporate_actions:
            if not action.get("affects_position", True):
                continue
            symbol = action["symbol"]
            effective_date = action.get("ex_date") or action["process_date"]
            values = multipliers(symbol, effective_date)
            if action["action_type"] in {"forward_split", "reverse_split"}:
                old_rate = float(action["old_rate"])
                new_rate = float(action["new_rate"])
                if old_rate <= 0 or new_rate <= 0:
                    raise BacktestDataError(f"{symbol} 拆股比例无效。")
                split_ratio = new_rate / old_rate
                values[0] /= split_ratio
                values[1] *= split_ratio

        for (symbol, effective_date), factor in self._dividend_factors.items():
            multipliers(symbol, effective_date)[0] *= float(factor)

        self._adjustment_dates: dict[str, list[str]] = {}
        self._adjustment_price_prefix: dict[str, list[float]] = {}
        self._adjustment_volume_prefix: dict[str, list[float]] = {}
        self._row_price_bases: dict[str, list[float]] = {}
        self._row_volume_bases: dict[str, list[float]] = {}
        self._canonical_daily: dict[str, list[dict]] = {}

        for symbol, rows in self.daily.items():
            dated = sorted(events.get(symbol, {}).items())
            dates: list[str] = []
            prices: list[float] = []
            volumes: list[float] = []
            price = 1.0
            volume = 1.0
            for effective_date, (price_step, volume_step) in dated:
                price *= price_step
                volume *= volume_step
                dates.append(effective_date)
                prices.append(price)
                volumes.append(volume)
            self._adjustment_dates[symbol] = dates
            self._adjustment_price_prefix[symbol] = prices
            self._adjustment_volume_prefix[symbol] = volumes

            row_price_bases: list[float] = []
            row_volume_bases: list[float] = []
            canonical: list[dict] = []
            for row in rows:
                row_price, row_volume = self._factors_as_of(symbol, row["date"])
                row_price_bases.append(row_price)
                row_volume_bases.append(row_volume)
                normalized = dict(row)
                for field in ("open", "high", "low", "close"):
                    normalized[field] = float(normalized[field]) / row_price
                normalized["volume"] = float(normalized.get("volume") or 0) / row_volume
                canonical.append(normalized)
            self._row_price_bases[symbol] = row_price_bases
            self._row_volume_bases[symbol] = row_volume_bases
            self._canonical_daily[symbol] = canonical

    def _factors_as_of(self, symbol: str, trading_date: str) -> tuple[float, float]:
        dates = self._adjustment_dates.get(symbol, [])
        index = bisect_right(dates, trading_date) - 1
        if index < 0:
            return 1.0, 1.0
        return (
            self._adjustment_price_prefix[symbol][index],
            self._adjustment_volume_prefix[symbol][index],
        )

    def is_eligible(self, symbol: str, trading_date: str) -> bool:
        start = self.availability_start.get(symbol)
        return bool(
            start
            and trading_date >= start
            and trading_date in self.daily_maps.get(symbol, {})
        )

    def active_symbols(self, symbols: Iterable[str], trading_date: str) -> list[str]:
        return [symbol for symbol in symbols if self.is_eligible(symbol, trading_date)]

    def cumulative_volume(self, symbol: str, trading_date: str, event: str) -> float:
        key = f"{trading_date}|{event}"
        try:
            return float(self.cumulative_volumes[symbol][key])
        except KeyError as exc:
            raise BacktestDataError(
                f"{symbol} 缺少 {trading_date} {event} 的盘中累计成交量。"
            ) from exc

    def _adjusted_slice(
        self,
        symbol: str,
        start: int,
        end: int,
        as_of_date: str,
    ) -> list[dict]:
        target_price, target_volume = self._factors_as_of(symbol, as_of_date)
        result: list[dict] = []
        rows = self.daily[symbol]
        price_bases = self._row_price_bases[symbol]
        volume_bases = self._row_volume_bases[symbol]
        for index in range(start, end):
            source = rows[index]
            price_factor = target_price / price_bases[index]
            volume_factor = target_volume / volume_bases[index]
            adjusted = dict(source)
            if abs(price_factor - 1.0) >= 1e-15:
                for field in ("open", "high", "low", "close"):
                    adjusted[field] = float(adjusted[field]) * price_factor
            if abs(volume_factor - 1.0) >= 1e-15:
                adjusted["volume"] = float(adjusted.get("volume") or 0) * volume_factor
            result.append(adjusted)
        return result

    def daily_before(
        self,
        symbol: str,
        trading_date: str,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        end = bisect_left(self.daily_dates[symbol], trading_date)
        start = 0 if limit is None else max(0, end - max(0, int(limit)))
        return self._adjusted_slice(symbol, start, end, trading_date)

    def indicator_history(
        self,
        symbol: str,
        trading_date: str,
        *,
        include_current: bool,
        limit: int | None = None,
    ) -> list[dict]:
        dates = self.daily_dates[symbol]
        end = (
            bisect_right(dates, trading_date)
            if include_current
            else bisect_left(dates, trading_date)
        )
        start = 0 if limit is None else max(0, end - max(0, int(limit)))
        return self._adjusted_slice(symbol, start, end, trading_date)

    def value_before(
        self,
        symbol: str,
        trading_date: str,
        field: str,
        periods_back: int,
    ) -> float:
        end = bisect_left(self.daily_dates[symbol], trading_date)
        index = end - int(periods_back)
        if index < 0:
            raise BacktestDataError(
                f"{symbol} 在 {trading_date} 之前没有足够数据计算 {field}({periods_back})。"
            )
        target_price, target_volume = self._factors_as_of(symbol, trading_date)
        if field == "volume":
            factor = target_volume / self._row_volume_bases[symbol][index]
        else:
            factor = target_price / self._row_price_bases[symbol][index]
        return float(self.daily[symbol][index][field]) * factor

    def _canonical_indicator_series(
        self,
        symbol: str,
        name: str,
        arguments: tuple[float | int, ...],
    ) -> list[float | None]:
        normalized = name.lower()
        key = (symbol, normalized, arguments)
        cached = self._indicator_series_cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        rows = self._canonical_daily[symbol]
        if normalized.startswith("macd_"):
            fast = int(arguments[0])
            slow = int(arguments[1])
            signal = int(arguments[2]) if len(arguments) == 3 else 9
            components = calculate_macd_components(rows, fast, slow, signal)
            component = {
                "macd_line": "line",
                "macd_signal": "signal",
                "macd_hist": "histogram",
            }[normalized]
            result = components[component]
        else:
            period = int(arguments[0])
            indicator_type = {
                "r_square": "LINEAR_FIT",
            }.get(normalized, normalized.upper())
            result = calculate_indicator_values(
                rows,
                indicator_type,
                period,
                half_life=(float(arguments[1]) if normalized == "wtme" else None),
                epsilon=(
                    float(arguments[2])
                    if normalized == "wtme" and len(arguments) == 3
                    else WTME_DEFAULT_EPSILON
                ),
                threshold_percent=(
                    float(arguments[1]) if normalized == "rapid_drop" else None
                ),
            )
        self._indicator_series_cache[key] = result
        return result

    def _volatility_prefixes(
        self,
        symbol: str,
    ) -> tuple[list[float], list[float], list[int]]:
        """Return prefix sums for O(1) point-in-time volatility windows."""
        cached = self._volatility_prefix_cache.get(symbol)
        if cached is not None:
            return cached
        sums = [0.0]
        squared = [0.0]
        invalid = [0]
        closes = [float(row["close"]) for row in self._canonical_daily[symbol]]
        self._volatility_date_index_cache[symbol] = {
            row["date"]: index
            for index, row in enumerate(self._canonical_daily[symbol])
        }
        for previous, current in zip(closes, closes[1:]):
            valid = (
                math.isfinite(previous)
                and math.isfinite(current)
                and previous > 0
                and current > 0
            )
            value = math.log(current / previous) if valid else 0.0
            sums.append(sums[-1] + value)
            squared.append(squared[-1] + value * value)
            invalid.append(invalid[-1] + (0 if valid else 1))
        result = (sums, squared, invalid)
        self._volatility_prefix_cache[symbol] = result
        return result

    def historical_volatility_value(
        self,
        symbol: str,
        trading_date: str,
        event: str,
        event_price: float,
        period: int,
    ) -> float | None:
        """Return VOLAT in O(1) after one O(history) prefix build per symbol.

        CLOSE uses the stored completed close.  OPEN and intraday decisions use
        ``period - 1`` completed close returns plus the return from the latest
        completed close to the price known at that event.
        """
        if period < 2:
            return None
        dates = self.daily_dates[symbol]
        sums, squared, invalid = self._volatility_prefixes(symbol)
        current_index = self._volatility_date_index_cache[symbol].get(trading_date)
        if event == "CLOSE":
            end_close = (
                current_index + 1
                if current_index is not None
                else bisect_right(dates, trading_date)
            )
            if end_close < period + 1:
                return None
            start_return = end_close - period - 1
            end_return = end_close - 1
            if invalid[end_return] - invalid[start_return]:
                return None
            total = sums[end_return] - sums[start_return]
            total_squared = squared[end_return] - squared[start_return]
        else:
            completed_end = (
                current_index
                if current_index is not None
                else bisect_left(dates, trading_date)
            )
            if completed_end < period:
                return None
            start_return = completed_end - period
            end_return = completed_end - 1
            if invalid[end_return] - invalid[start_return]:
                return None
            total = sums[end_return] - sums[start_return]
            total_squared = squared[end_return] - squared[start_return]
            target_price, _ = self._factors_as_of(symbol, trading_date)
            current = float(event_price) / target_price
            previous = float(self._canonical_daily[symbol][completed_end - 1]["close"])
            if not (
                math.isfinite(current)
                and math.isfinite(previous)
                and current > 0
                and previous > 0
            ):
                return None
            newest = math.log(current / previous)
            total += newest
            total_squared += newest * newest
        variance = (total_squared - total * total / period) / (period - 1)
        tolerance = 1e-18 * max(1.0, abs(total_squared))
        if variance < -tolerance:
            return None
        return math.sqrt(max(0.0, variance)) * math.sqrt(252) * 100

    def indicator_value(
        self,
        symbol: str,
        trading_date: str,
        event: str,
        event_price: float,
        name: str,
        arguments: tuple[float | int, ...],
    ) -> float | None:
        """Return one point-in-time indicator value without rebuilding history."""
        normalized = name.lower()
        period = int(arguments[0])
        include_current = event == "CLOSE"

        if normalized == "volat":
            return self.historical_volatility_value(
                symbol,
                trading_date,
                event,
                event_price,
                period,
            )

        # These intraday indicators deliberately include a synthetic unfinished
        # current bar.  They are finite-window formulas, so only that window is
        # constructed and evaluated.
        if not include_current and normalized in {"r_square", "wtme", "rapid_drop"}:
            completed = self.daily_before(symbol, trading_date, limit=period)
            if len(completed) < period:
                return None
            previous_close = float(completed[-1]["close"])
            rows = [
                *completed,
                {
                    "date": trading_date,
                    "open": previous_close,
                    "high": max(previous_close, float(event_price)),
                    "low": min(previous_close, float(event_price)),
                    "close": float(event_price),
                    "volume": 0.0,
                    "is_complete": 0,
                },
            ]
            indicator_type = "LINEAR_FIT" if normalized == "r_square" else normalized.upper()
            values = calculate_indicator_values(
                rows,
                indicator_type,
                period,
                half_life=(float(arguments[1]) if normalized == "wtme" else None),
                epsilon=(
                    float(arguments[2])
                    if normalized == "wtme" and len(arguments) == 3
                    else WTME_DEFAULT_EPSILON
                ),
                threshold_percent=(
                    float(arguments[1]) if normalized == "rapid_drop" else None
                ),
            )
            return values[-1] if values else None

        end = (
            bisect_right(self.daily_dates[symbol], trading_date)
            if include_current
            else bisect_left(self.daily_dates[symbol], trading_date)
        )
        if end <= 0:
            return None

        if not include_current and normalized == "ratr":
            base_index = end - period
            if base_index < 0:
                return None
            atr_values = self._canonical_indicator_series(symbol, "atr", (period,))
            atr = atr_values[end - 1]
            if atr is None or atr <= 0:
                return None
            price_factor, _ = self._factors_as_of(symbol, trading_date)
            current = float(event_price) / price_factor
            base = float(self._canonical_daily[symbol][base_index]["close"])
            return (current - base) / float(atr)

        values = self._canonical_indicator_series(symbol, normalized, arguments)
        value = values[end - 1]
        if value is None:
            return None
        if normalized in {"ma", "ema", "atr", "macd_line", "macd_signal", "macd_hist"}:
            price_factor, _ = self._factors_as_of(symbol, trading_date)
            return float(value) * price_factor
        return float(value)

    def _strategy_atr_series(
        self,
        symbol: str,
        period: int,
        weighting: str,
    ) -> list[float | None]:
        key = (symbol, int(period), str(weighting))
        cached = self._strategy_atr_cache.get(key)
        if cached is not None:
            return cached
        rows = self._canonical_daily[symbol]
        result: list[float | None] = [None] * len(rows)
        true_ranges = [
            max(
                float(current["high"]) - float(current["low"]),
                abs(float(current["high"]) - float(previous["close"])),
                abs(float(current["low"]) - float(previous["close"])),
            )
            for previous, current in zip(rows, rows[1:])
        ]
        if len(true_ranges) < period:
            self._strategy_atr_cache[key] = result
            return result
        if weighting == "simple":
            window_sum = sum(true_ranges[:period])
            result[period] = window_sum / period
            for row_index in range(period + 1, len(rows)):
                window_sum += true_ranges[row_index - 1] - true_ranges[row_index - period - 1]
                result[row_index] = window_sum / period
        elif weighting == "linear":
            denominator = period * (period + 1) / 2
            window = true_ranges[:period]
            window_sum = sum(window)
            weighted_sum = sum(index * value for index, value in enumerate(window, start=1))
            result[period] = weighted_sum / denominator
            for row_index in range(period + 1, len(rows)):
                newest = true_ranges[row_index - 1]
                weighted_sum = weighted_sum - window_sum + period * newest
                window_sum += newest - true_ranges[row_index - period - 1]
                result[row_index] = weighted_sum / denominator
        else:
            atr = sum(true_ranges[:period]) / period
            result[period] = atr
            alpha = 1 / period if weighting == "wilder" else 2 / (period + 1)
            for row_index, true_range in enumerate(
                true_ranges[period:], start=period + 1
            ):
                atr = alpha * true_range + (1 - alpha) * atr
                result[row_index] = atr
        self._strategy_atr_cache[key] = result
        return result

    def strategy_atr_values_before(
        self,
        symbol: str,
        trading_date: str,
        period: int,
        weighting: str,
        *,
        limit: int,
    ) -> list[float | None]:
        end = bisect_left(self.daily_dates[symbol], trading_date)
        start = max(0, end - int(limit))
        price_factor, _ = self._factors_as_of(symbol, trading_date)
        return [
            None if value is None else float(value) * price_factor
            for value in self._strategy_atr_series(symbol, period, weighting)[start:end]
        ]

    def strategy_atr_value_before(
        self,
        symbol: str,
        trading_date: str,
        period: int,
        weighting: str,
    ) -> float | None:
        values = self.strategy_atr_values_before(
            symbol, trading_date, period, weighting, limit=1
        )
        return values[-1] if values else None

    def _adjust_row(self, symbol: str, row: dict, as_of_date: str) -> dict:
        index = bisect_left(self.daily_dates[symbol], row["date"])
        if index >= len(self.daily[symbol]) or self.daily[symbol][index]["date"] != row["date"]:
            raise BacktestDataError(f"{symbol} 缺少 {row['date']} 日线数据。")
        return self._adjusted_slice(symbol, index, index + 1, as_of_date)[0]

    def corporate_actions_on(self, trading_date: str) -> list[dict]:
        return list(self.actions_by_date.get(trading_date, []))

    def day_bar(self, symbol: str, trading_date: str) -> dict:
        try:
            return self.daily_maps[symbol][trading_date]
        except KeyError as exc:
            raise BacktestDataError(f"{symbol} 缺少 {trading_date} 日线数据。") from exc

    def previous_session(self, trading_date: str) -> str | None:
        index = self._session_index.get(trading_date)
        if index is None or index == 0:
            return None
        return self.sessions[index - 1]

    def next_session(self, trading_date: str) -> str | None:
        index = self._session_index.get(trading_date)
        if index is None or index + 1 >= len(self.sessions):
            return None
        return self.sessions[index + 1]

    def session_minutes(self, trading_date: str) -> tuple[int, int]:
        return self._market_session_minutes.get(trading_date, (
            _epoch_minute(trading_date, "09:30"),
            _epoch_minute(trading_date, "16:00"),
        ))

    def event_price(self, symbol: str, trading_date: str, event: str) -> EventPrice:
        day = self.day_bar(symbol, trading_date)
        if event == "CLOSE":
            return EventPrice(
                signal_price=float(day["close"]),
                fill_price=None,
                signal_time=f"{trading_date} 16:00 America/New_York",
                fill_time=None,
            )
        if event == "OPEN":
            try:
                previous_close = self.value_before(
                    symbol, trading_date, "close", 1
                )
            except BacktestDataError:
                raise BacktestDataError(
                    f"{symbol} 在 {trading_date} 开盘前没有可用收盘价。"
                ) from None
            return EventPrice(
                signal_price=previous_close,
                fill_price=float(day["open"]),
                signal_time=f"{trading_date} 09:29:59 America/New_York",
                fill_time=f"{trading_date} 09:30 America/New_York",
            )
        current_minute = _epoch_minute(trading_date, event)
        resolution = self.intraday_event_minutes.get(symbol, {}).get(
            f"{trading_date}|{event}",
            {},
        )
        previous_minute = int(resolution.get("signal_minute", current_minute - 1))
        fill_minute = int(resolution.get("fill_minute", current_minute))
        bars = self.minute.get(symbol, {})
        if previous_minute not in bars:
            raise BacktestDataError(
                f"{symbol} 缺少 {trading_date} {event} 决策所需的上一分钟行情。"
            )
        if fill_minute not in bars:
            raise BacktestDataError(
                f"{symbol} 缺少 {trading_date} {event} 的成交分钟行情。"
            )
        return EventPrice(
            signal_price=float(bars[previous_minute]["close"]),
            fill_price=float(bars[fill_minute]["open"]),
            signal_time=_minute_label(previous_minute),
            fill_time=_minute_label(fill_minute),
        )

    def expression_context(
        self,
        *,
        symbol: str,
        trading_date: str,
        event: str,
        price: float,
        position: float,
    ) -> ExpressionContext:
        return ExpressionContext(
            self,
            symbol=symbol,
            trading_date=trading_date,
            event=event,
            price=price,
            position=position,
        )

    def close_prices(self, trading_date: str, symbols: Iterable[str]) -> dict[str, float]:
        return {
            symbol: float(self.day_bar(symbol, trading_date)["close"])
            for symbol in symbols
        }

    def open_prices(self, trading_date: str, symbols: Iterable[str]) -> dict[str, float]:
        return {
            symbol: float(self.day_bar(symbol, trading_date)["open"])
            for symbol in symbols
        }


def _minute_failure_segments(
    failures: list[dict],
    sessions: list[str],
) -> list[dict]:
    session_index = {value: index for index, value in enumerate(sessions)}
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for item in failures:
        key = (item["event"], tuple(item["missing"]))
        groups.setdefault(key, []).append(item["trading_date"])
    segments = []
    for (event, missing), dates in groups.items():
        ordered = sorted(set(dates), key=session_index.get)
        start = previous = ordered[0]
        count = 1
        for trading_date in ordered[1:]:
            if session_index[trading_date] == session_index[previous] + 1:
                previous = trading_date
                count += 1
                continue
            segments.append(
                {
                    "start_date": start,
                    "end_date": previous,
                    "event": event,
                    "missing": list(missing),
                    "missing_session_count": count,
                }
            )
            start = previous = trading_date
            count = 1
        segments.append(
            {
                "start_date": start,
                "end_date": previous,
                "event": event,
                "missing": list(missing),
                "missing_session_count": count,
            }
        )
    return segments


def _minute_failure_summary(items: list[dict]) -> str:
    labels = {"signal": "事件前信号行情", "fill": "事件后可成交行情"}
    parts = []
    for item in items:
        symbol = item.get("symbol", "未知标的")
        segments = item.get("segments") or []
        if not segments:
            parts.append(f"{symbol} 存在无效分钟记录")
            continue
        descriptions = []
        for segment in segments[:4]:
            date_text = segment["start_date"]
            if segment["end_date"] != segment["start_date"]:
                date_text += f" 至 {segment['end_date']}"
            missing_text = "、".join(
                labels.get(value, value)
                for value in segment["missing"]
            )
            descriptions.append(
                f"{date_text} {segment['event']} 缺少{missing_text}"
            )
        if len(segments) > 4:
            descriptions.append(f"另有 {len(segments) - 4} 段")
        parts.append(f"{symbol}：" + "；".join(descriptions))
    return "；".join(parts)


def load_historical_dataset(
    *,
    universe: list[str],
    additional_symbols: list[str],
    start_date: str,
    end_date: str,
    intraday_events: Iterable[str],
    minimum_lookback: int,
    early_close_offsets: dict[str, int] | None = None,
    cumulative_volume_events: Iterable[str] = (),
    optional_symbols: Iterable[str] = (),
    market: dict | str | None = None,
) -> HistoricalDataSet:
    market_config = normalize_market_config(market)
    symbols = list(dict.fromkeys([*universe, *additional_symbols]))
    optional_symbols = set(optional_symbols)
    if not universe:
        raise BacktestValidationError("回测标的池不能为空。")
    daily = {
        symbol: repository.get_strategy_daily_prices(
            symbol,
            market_config["type"],
            include_metadata=True,
        )
        for symbol in symbols
    }
    empty_required = [
        symbol
        for symbol, rows in daily.items()
        if not rows and symbol not in optional_symbols
    ]
    if empty_required:
        symbol = empty_required[0]
        raise BacktestDataError(
            f"标的 {symbol} 没有本地日线行情；缺失时间从 {start_date} 开始。",
            detail={
                "symbol": symbol,
                "type": "daily_missing",
                "missing_date": start_date,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
    earliest_daily = min(
        str(rows[0]["date"])
        for rows in daily.values()
        if rows
    )
    all_market_sessions = ensure_market_sessions(
        min(earliest_daily, start_date),
        end_date,
    )
    all_session_dates = {
        str(item["trading_date"]) for item in all_market_sessions
    }
    calendar_first = min(all_session_dates) if all_session_dates else start_date
    daily = {
        symbol: [
            row for row in rows
            if str(row["date"]) in all_session_dates
            or str(row["date"]) < calendar_first
        ]
        for symbol, rows in daily.items()
    }
    market_sessions = [
        item for item in all_market_sessions
        if start_date <= str(item["trading_date"]) <= end_date
    ]
    sessions = [item["trading_date"] for item in market_sessions]
    if not sessions:
        raise BacktestDataError("所选回测区间没有交易日数据。")
    latest_safe_minute = int(datetime.now(timezone.utc).timestamp()) // 60 - 20
    unfinished_sessions = [
        item["trading_date"]
        for item in market_sessions
        if int(item["close_minute_utc"]) > latest_safe_minute
    ]
    if unfinished_sessions:
        raise BacktestDataError(
            "回测区间包含尚未结束或延迟行情尚未完成的交易日。",
            detail={"dates": unfinished_sessions},
        )

    availability_start: dict[str, str | None] = {}
    for symbol in symbols:
        if not daily[symbol]:
            availability_start[symbol] = None
            continue
        invalid_daily = [
            issue
            for row in daily[symbol]
            if row["date"] <= sessions[-1]
            if (issue := _validate_bar(row, symbol=symbol, granularity="daily"))
        ]
        if invalid_daily:
            issue = invalid_daily[0]
            raise BacktestDataError(
                f"标的 {symbol} 在 {issue['at']} 的日线行情无效：{issue['reason']}。",
                detail=issue,
            )
        rows_by_date = {row["date"]: row for row in daily[symbol]}
        dates = set(rows_by_date)
        first_stored_date = min(dates)
        first_in_range = next((value for value in sessions if value in dates), None)
        if first_in_range is None:
            availability_start[symbol] = None
            starts_after_range = first_stored_date > sessions[-1]
            if symbol not in optional_symbols and not starts_after_range:
                raise BacktestDataError(
                    f"标的 {symbol} 在回测区间 {sessions[0]} 至 {sessions[-1]} 没有日线行情。",
                    detail={
                        "symbol": symbol,
                        "type": "no_data_in_range",
                        "missing_date": sessions[0],
                        "start_date": sessions[0],
                        "end_date": sessions[-1],
                        "history_start_date": first_stored_date,
                    },
                )
            continue
        eligible = next(
            (
                value
                for value in sessions
                if value in dates
                and sum(1 for row in daily[symbol] if row["date"] < value)
                >= minimum_lookback
            ),
            None,
        )
        availability_start[symbol] = eligible
        if eligible is None:
            if symbol not in optional_symbols:
                raise BacktestDataError(
                    f"标的 {symbol} 截至 {sessions[-1]} 仍不足 {minimum_lookback} 个预热交易日。",
                    detail={
                        "symbol": symbol,
                        "type": "warmup_never_reached",
                        "required": minimum_lookback,
                        "history_start_date": first_stored_date,
                        "missing_date": sessions[-1],
                    },
                )
            continue
        # Missing dates before the first stored row are a valid late inception.
        # Any hole after trading begins remains a strict data-integrity failure.
        validation_start = (
            sessions[0]
            if any(row["date"] < sessions[0] for row in daily[symbol])
            else first_in_range
        )
        absent = [
            value
            for value in sessions
            if value >= validation_start and value not in dates
        ]
        if absent:
            missing_date = absent[0]
            raise BacktestDataError(
                f"标的 {symbol} 缺少 {missing_date} 的日线行情。",
                detail={
                    "symbol": symbol,
                    "type": "daily",
                    "missing_date": missing_date,
                    "missing_count": len(absent),
                },
            )
        incomplete = [
            value
            for value in sessions
            if value in rows_by_date
            and not bool(rows_by_date[value].get("is_complete", True))
        ]
        if incomplete:
            missing_date = incomplete[0]
            raise BacktestDataError(
                f"标的 {symbol} 在 {missing_date} 的日线数据尚未完成。",
                detail={
                    "symbol": symbol,
                    "type": "daily_incomplete",
                    "missing_date": missing_date,
                    "incomplete_count": len(incomplete),
                },
            )

    symbol_settings = {}
    for symbol in symbols:
        try:
            symbol_settings[symbol] = repository.get_symbol(symbol)
        except Exception:
            # Pure/supplied dataset tests do not require a configured catalog DB.
            # The data snapshot still records deterministic inferred metadata.
            symbol_settings[symbol] = {
                "asset_class": "crypto" if symbol == "BTC/USD" else "us_equity",
                "quantity_step": 0.0001 if symbol == "BTC/USD" else None,
                "history_start_date": daily[symbol][0]["date"] if daily[symbol] else None,
            }
    corporate_action_symbols = [
        symbol for symbol in symbols
        if daily[symbol]
        and symbol_settings[symbol].get("asset_class") == "us_equity"
    ]
    action_starts: dict[str, str] = {}
    for symbol in corporate_action_symbols:
        prior_rows = [
            row for row in daily[symbol]
            if row["date"] < sessions[0]
        ]
        if minimum_lookback > 0 and prior_rows:
            relevant_index = max(0, len(prior_rows) - minimum_lookback)
            relevant_start = prior_rows[relevant_index]["date"]
        else:
            relevant_start = sessions[0]
        # The actual first local bar is also the identity lower bound. This
        # excludes an earlier issuer that reused the same ticker (MAGS).
        action_starts[symbol] = max(
            daily[symbol][0]["date"],
            relevant_start,
        )
    corporate_action_start = min(action_starts.values(), default=sessions[0])
    corporate_actions = ensure_corporate_actions(
        corporate_action_symbols,
        start_date=corporate_action_start,
        end_date=sessions[-1],
        symbol_starts=action_starts,
    )
    validate_supported_actions(corporate_actions)
    action_symbols = {
        action["symbol"]
        for action in corporate_actions
        if action.get("affects_position", True)
    }
    for symbol in sorted(action_symbols):
        invalid_basis = sorted(
            {
                str(row.get("price_basis") or "raw")
                for row in daily[symbol]
                if action_starts[symbol] <= row["date"] <= sessions[-1]
                if str(row.get("price_basis") or "raw") != "raw"
            }
        )
        if invalid_basis:
            raise BacktestDataError(
                f"{symbol} 的历史行情不是可核验的原始价格，不能安全应用公司行动。",
                detail={
                    "symbol": symbol,
                    "type": "price_basis_not_raw",
                    "price_basis": invalid_basis,
                },
            )

    exact_events = sorted(
        {
            event
            for event in intraday_events
            if event not in {"OPEN", "CLOSE"}
        }
    )
    daily_availability_start = dict(availability_start)
    minute_history_metadata: dict[str, dict] = {}
    intraday_join_dates: dict[str, str] = {}
    if exact_events:
        for symbol in universe:
            if uses_previous_close_for_historical_intraday(symbol, market_config):
                minute_history_metadata[symbol] = {
                    "minute_history_start_date": None,
                    "minute_history_start_source": "previous_session_close_fallback",
                    "minute_history_start_verified": True,
                }
                continue
            sync_state = intraday_repository.get_sync_state(symbol)
            minute_history_metadata[symbol] = {
                "minute_history_start_date": sync_state.get(
                    "minute_history_start_date"
                ),
                "minute_history_start_source": sync_state.get(
                    "minute_history_start_source"
                ),
                "minute_history_start_verified": bool(
                    sync_state.get("minute_history_start_verified")
                ),
            }
            minute_start = (
                sync_state.get("minute_history_start_date")
                if sync_state.get("minute_history_start_verified")
                else None
            )
            daily_start = availability_start.get(symbol)
            if not minute_start or not daily_start:
                continue
            combined_start = max(daily_start, str(minute_start))
            daily_dates = {row["date"] for row in daily[symbol]}
            joined_session = next(
                (
                    trading_date
                    for trading_date in sessions
                    if trading_date >= combined_start
                    and trading_date in daily_dates
                ),
                None,
            )
            availability_start[symbol] = joined_session
            if joined_session and (
                joined_session > daily_start or joined_session > sessions[0]
            ):
                intraday_join_dates[symbol] = joined_session
    early_close_offsets = dict(early_close_offsets or {})
    cumulative_volume_events = set(cumulative_volume_events)
    session_by_date = {item["trading_date"]: item for item in market_sessions}

    def effective_event_minute(session: dict, event: str) -> int:
        target = _epoch_minute(session["trading_date"], event)
        if int(session["open_minute_utc"]) <= target < int(session["close_minute_utc"]):
            return target
        if event in early_close_offsets and bool(session.get("is_early_close")):
            return int(session["close_minute_utc"]) - int(early_close_offsets[event])
        return target

    invalid_early_close_events = []
    for session in market_sessions:
        for event in exact_events:
            event_minute = effective_event_minute(session, event)
            if not (
                int(session["open_minute_utc"])
                <= event_minute
                < int(session["close_minute_utc"])
            ):
                invalid_early_close_events.append(
                    {
                        "trading_date": session["trading_date"],
                        "event": event,
                        "close_time": datetime.fromtimestamp(
                            int(session["close_minute_utc"]) * 60,
                            tz=timezone.utc,
                        ).astimezone(NEW_YORK).strftime("%H:%M"),
                    }
                )
    if invalid_early_close_events:
        raise BacktestDataError(
            "策略具体时间超出部分交易日的实际交易时段。",
            detail=invalid_early_close_events,
        )
    minute: dict[str, dict[int, dict]] = {}
    intraday_event_minutes: dict[str, dict[str, dict]] = {}
    cumulative_volumes: dict[str, dict[str, float]] = {}
    delayed_intraday_events: dict[str, list[dict]] = {}
    intraday_price_fallbacks: dict[str, dict] = {}
    minute_missing: list[dict] = []
    if exact_events:
        for symbol in universe:
            eligible_sessions = [
                trading_date for trading_date in sessions
                if availability_start.get(symbol)
                and trading_date >= availability_start[symbol]
            ]
            if uses_previous_close_for_historical_intraday(symbol, market_config):
                bars: dict[int, dict] = {}
                resolutions: dict[str, dict] = {}
                rows = daily[symbol]
                for trading_date in eligible_sessions:
                    previous = next(
                        (
                            row for row in reversed(rows)
                            if str(row["date"]) < trading_date
                            and bool(row.get("is_complete", True))
                        ),
                        None,
                    )
                    if previous is None:
                        minute_missing.append({
                            "symbol": symbol,
                            "type": "minute_previous_close_fallback",
                            "missing_date": trading_date,
                        })
                        continue
                    previous_close = float(previous["close"])
                    session = session_by_date[trading_date]
                    for event in exact_events:
                        target = effective_event_minute(session, event)
                        for minute_value in (target - 1, target):
                            bars[minute_value] = {
                                "minute_utc": minute_value,
                                "open": previous_close,
                                "high": previous_close,
                                "low": previous_close,
                                "close": previous_close,
                                "volume": 0.0,
                                "trade_count": 0,
                                "vwap": previous_close,
                            }
                        resolutions[f"{trading_date}|{event}"] = {
                            "signal_minute": target - 1,
                            "fill_minute": target,
                        }
                        if event in cumulative_volume_events:
                            cumulative_volumes.setdefault(symbol, {})[
                                f"{trading_date}|{event}"
                            ] = 0.0
                minute[symbol] = bars
                intraday_event_minutes[symbol] = resolutions
                intraday_price_fallbacks[symbol] = {
                    "mode": "previous_session_close",
                    "reason": f"{symbol} has no Alpaca minute history",
                    "events": list(exact_events),
                }
                continue
            required_minutes = sorted(
                {
                    minute_value
                    for trading_date in eligible_sessions
                    for event in exact_events
                    for minute_value in (
                        effective_event_minute(session_by_date[trading_date], event) - 1,
                        effective_event_minute(session_by_date[trading_date], event),
                    )
                }
            )
            bars = intraday_repository.get_minute_bars_at(symbol, required_minutes)
            resolutions: dict[str, dict] = {}
            gap_requests = []
            request_context: dict[int, tuple[str, str]] = {}
            for trading_date in eligible_sessions:
                session = session_by_date[trading_date]
                for event in exact_events:
                    target = effective_event_minute(session, event)
                    key = f"{trading_date}|{event}"
                    if target - 1 in bars and target in bars:
                        resolutions[key] = {
                            "signal_minute": target - 1,
                            "fill_minute": target,
                        }
                    else:
                        gap_requests.append(
                            {
                                "target_minute": target,
                                "open_minute": int(session["open_minute_utc"]),
                                "close_minute": int(session["close_minute_utc"]),
                            }
                        )
                        request_context[target] = (trading_date, event)
            gap_resolutions = intraday_repository.resolve_minute_event_gaps(
                symbol,
                gap_requests,
            )
            additional_minutes = set()
            failures = []
            delayed = []
            for request in gap_requests:
                target = int(request["target_minute"])
                trading_date, event = request_context[target]
                resolved = gap_resolutions.get(target, {})
                signal_minute = resolved.get("signal_minute")
                fill_minute = resolved.get("fill_minute")
                missing_parts = []
                if signal_minute is None:
                    missing_parts.append("signal")
                else:
                    additional_minutes.add(int(signal_minute))
                if fill_minute is None:
                    missing_parts.append("fill")
                else:
                    additional_minutes.add(int(fill_minute))
                if missing_parts:
                    failures.append(
                        {
                            "trading_date": trading_date,
                            "event": event,
                            "missing": missing_parts,
                        }
                    )
                    continue
                resolutions[f"{trading_date}|{event}"] = {
                    "signal_minute": int(signal_minute),
                    "fill_minute": int(fill_minute),
                }
                delayed.append(
                    {
                        "trading_date": trading_date,
                        "event": event,
                        "signal_time": _minute_label(int(signal_minute)),
                        "fill_time": _minute_label(int(fill_minute)),
                    }
                )
            if additional_minutes:
                bars.update(
                    intraday_repository.get_minute_bars_at(
                        symbol,
                        sorted(additional_minutes),
                    )
                )
            minute[symbol] = bars
            intraday_event_minutes[symbol] = resolutions
            volume_requests = []
            for trading_date in eligible_sessions:
                session = session_by_date[trading_date]
                for event in cumulative_volume_events:
                    resolution = resolutions.get(f"{trading_date}|{event}")
                    if resolution:
                        volume_requests.append(
                            {
                                "key": f"{trading_date}|{event}",
                                "start_minute": int(session["open_minute_utc"]),
                                "end_minute": int(resolution["signal_minute"]) + 1,
                            }
                        )
            cumulative_volumes[symbol] = intraday_repository.get_cumulative_volumes(
                symbol, volume_requests
            )
            if delayed:
                delayed_intraday_events[symbol] = delayed
            invalid_minute = [
                issue
                for row in bars.values()
                if (
                    issue := _validate_bar(
                        row,
                        symbol=symbol,
                        granularity="minute",
                    )
                )
            ]
            if invalid_minute:
                minute_missing.extend(invalid_minute[:20])
            if failures:
                minute_missing.append(
                    {
                        "symbol": symbol,
                        "type": "minute",
                        "missing_count": len(failures),
                        "segments": _minute_failure_segments(failures, sessions),
                        "examples": failures[:20],
                    }
                )
    if minute_missing:
        summary = _minute_failure_summary(minute_missing)
        raise BacktestDataError(
            f"分钟数据不完整：{summary}",
            detail=minute_missing,
        )

    manifest_symbols = {}
    for symbol in symbols:
        relevant_daily = [
            {
                key: row.get(key)
                for key in ("date", "open", "high", "low", "close", "volume")
            }
            for row in daily[symbol]
            if row["date"] <= sessions[-1]
        ]
        relevant_minute = [
            {
                key: row.get(key)
                for key in (
                    "minute_utc",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "trade_count",
                    "vwap",
                )
            }
            for _, row in sorted(minute.get(symbol, {}).items())
        ]
        relevant_cumulative_volumes = [
            {"event": key, "volume": value}
            for key, value in sorted(cumulative_volumes.get(symbol, {}).items())
        ]
        manifest_symbols[symbol] = {
            "daily_series": strategy_daily_series(symbol, market_config),
            "daily_first": (
                relevant_daily[0]["date"] if relevant_daily else None
            ),
            "daily_last": (
                relevant_daily[-1]["date"] if relevant_daily else None
            ),
            "daily_rows": len(relevant_daily),
            "daily_sha256": _sha256(relevant_daily),
            "minute_points_loaded": len(relevant_minute),
            "minute_sha256": _sha256(relevant_minute),
            "cumulative_volume_points": len(relevant_cumulative_volumes),
            "cumulative_volume_sha256": _sha256(relevant_cumulative_volumes),
            "daily_history_start_date": (
                relevant_daily[0]["date"] if relevant_daily else None
            ),
            "daily_eligible_start_date": daily_availability_start.get(symbol),
            **minute_history_metadata.get(symbol, {}),
            "intraday_join_date": intraday_join_dates.get(symbol),
            "intraday_price_fallback": intraday_price_fallbacks.get(symbol),
            # Compatibility alias for older exported manifests.
            "history_start_date": (
                relevant_daily[0]["date"] if relevant_daily else None
            ),
            "eligible_start_date": availability_start.get(symbol),
            "asset_class": symbol_settings[symbol].get("asset_class") or "us_equity",
            "quantity_step": symbol_settings[symbol].get("quantity_step"),
        }
    normalized_actions = [
        {
            "provider_id": action["provider_id"],
            "symbol": action["symbol"],
            "action_type": action["action_type"],
            "effective_date": action.get("ex_date") or action["process_date"],
            "payable_date": action.get("payable_date"),
            "old_rate": action.get("old_rate"),
            "new_rate": action.get("new_rate"),
            "cash_rate": action.get("cash_rate"),
            "event_symbol": action.get("event_symbol"),
            "matched_role": action.get("matched_role"),
            "affects_position": action.get("affects_position", True),
        }
        for action in corporate_actions
    ]
    normalized_sessions = [
        {
            "trading_date": item["trading_date"],
            "open_minute_utc": item["open_minute_utc"],
            "close_minute_utc": item["close_minute_utc"],
            "is_early_close": bool(item.get("is_early_close")),
        }
        for item in market_sessions
    ]
    manifest = {
        "data_contract_version": 3,
        "market": calendar_contract(market_config),
        "symbols": {
            symbol: manifest_symbols[symbol]
            for symbol in symbols
        },
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "sessions": len(sessions),
        "intraday_events": exact_events,
        "cumulative_volume_events": sorted(cumulative_volume_events),
        "early_close_event_offsets": early_close_offsets,
        "delayed_intraday_events": delayed_intraday_events,
        "intraday_price_fallbacks": intraday_price_fallbacks,
        "minimum_lookback": minimum_lookback,
        "timezone": market_config["timezone"],
        "early_close_sessions": [
            item["trading_date"]
            for item in market_sessions
            if item.get("is_early_close")
        ],
        "market_calendar_sha256": _sha256(normalized_sessions),
        "market_sessions": normalized_sessions,
        "corporate_actions": normalized_actions,
        "corporate_actions_sha256": _sha256(normalized_actions),
    }
    return HistoricalDataSet(
        daily=daily,
        sessions=sessions,
        minute=minute,
        intraday_event_minutes=intraday_event_minutes,
        cumulative_volumes=cumulative_volumes,
        availability_start=availability_start,
        required_intraday_events=exact_events,
        corporate_actions=corporate_actions,
        manifest=manifest,
    )
