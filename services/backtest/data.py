from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from statistics import fmean
from typing import Iterable
from zoneinfo import ZoneInfo

from database import intraday_repository, repository
from services.backtest.corporate_actions import (
    ensure_corporate_actions,
    validate_supported_actions,
)
from services.backtest.errors import BacktestDataError, BacktestValidationError
from services.backtest.market_calendar import ensure_market_sessions


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

    def resolve_function(self, name: str, period: int) -> float:
        if name in {"open", "high", "low", "close", "volume"}:
            rows = self.dataset.daily_before(self.symbol, self.trading_date)
            if len(rows) < period:
                raise BacktestDataError(
                    f"{self.symbol} 在 {self.trading_date} 之前没有足够数据计算 {name}({period})。"
                )
            return float(rows[-period][name])
        rows = self.dataset.indicator_history(
            self.symbol,
            self.trading_date,
            include_current=self.event == "CLOSE",
        )
        if name == "ma":
            if len(rows) < period:
                raise BacktestDataError(
                    f"{self.symbol} 没有足够数据计算 ma({period})。"
                )
            return fmean(float(row["close"]) for row in rows[-period:])
        if name == "ema":
            if len(rows) < period:
                raise BacktestDataError(
                    f"{self.symbol} 没有足够数据计算 ema({period})。"
                )
            alpha = 2.0 / (period + 1.0)
            value = float(rows[0]["close"])
            for row in rows[1:]:
                value = alpha * float(row["close"]) + (1 - alpha) * value
            return value
        if name == "atr":
            if len(rows) < period + 1:
                raise BacktestDataError(
                    f"{self.symbol} 没有足够数据计算 atr({period})。"
                )
            true_ranges: list[float] = []
            for previous, current in zip(rows, rows[1:]):
                high = float(current["high"])
                low = float(current["low"])
                previous_close = float(previous["close"])
                true_ranges.append(
                    max(
                        high - low,
                        abs(high - previous_close),
                        abs(low - previous_close),
                    )
                )
            atr = fmean(true_ranges[:period])
            for true_range in true_ranges[period:]:
                atr = ((period - 1) * atr + true_range) / period
            return atr
        raise BacktestValidationError(f"不支持指标函数 {name}。")


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
        self.manifest = manifest or {}
        self._session_index = {value: index for index, value in enumerate(self.sessions)}

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

    def daily_before(self, symbol: str, trading_date: str) -> list[dict]:
        return [
            self._adjust_row(symbol, row, trading_date)
            for row in self.daily[symbol]
            if row["date"] < trading_date
        ]

    def indicator_history(
        self,
        symbol: str,
        trading_date: str,
        *,
        include_current: bool,
    ) -> list[dict]:
        if include_current:
            return [
                self._adjust_row(symbol, row, trading_date)
                for row in self.daily[symbol]
                if row["date"] <= trading_date
            ]
        return self.daily_before(symbol, trading_date)

    def _adjust_row(self, symbol: str, row: dict, as_of_date: str) -> dict:
        factor = 1.0
        for action in self.corporate_actions:
            if action["symbol"] != symbol:
                continue
            if action["action_type"] not in {"forward_split", "reverse_split"}:
                continue
            effective_date = action.get("ex_date") or action["process_date"]
            if row["date"] < effective_date <= as_of_date:
                old_rate = float(action["old_rate"])
                new_rate = float(action["new_rate"])
                if old_rate <= 0 or new_rate <= 0:
                    raise BacktestDataError(f"{symbol} 拆股比例无效。")
                factor *= new_rate / old_rate
        if abs(factor - 1.0) < 1e-15:
            return dict(row)
        adjusted = dict(row)
        for field in ("open", "high", "low", "close"):
            adjusted[field] = float(adjusted[field]) / factor
        adjusted["volume"] = float(adjusted.get("volume") or 0) * factor
        return adjusted

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
            previous_rows = self.daily_before(symbol, trading_date)
            if not previous_rows:
                raise BacktestDataError(
                    f"{symbol} 在 {trading_date} 开盘前没有可用收盘价。"
                )
            return EventPrice(
                signal_price=float(previous_rows[-1]["close"]),
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
) -> HistoricalDataSet:
    symbols = list(dict.fromkeys([*universe, *additional_symbols]))
    optional_symbols = set(optional_symbols)
    if not universe:
        raise BacktestValidationError("回测标的池不能为空。")
    daily = {
        symbol: repository.get_daily_prices(symbol, include_metadata=True)
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
    market_sessions = ensure_market_sessions(start_date, end_date)
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

    corporate_action_start = min(
        rows[0]["date"]
        for rows in daily.values()
        if rows
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
        and symbol_settings[symbol].get("asset_class") != "crypto"
    ]
    corporate_actions = ensure_corporate_actions(
        corporate_action_symbols,
        start_date=corporate_action_start,
        end_date=sessions[-1],
    )
    validate_supported_actions(corporate_actions)

    exact_events = sorted(
        {
            event
            for event in intraday_events
            if event not in {"OPEN", "CLOSE"}
        }
    )
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
    minute_missing: list[dict] = []
    if exact_events:
        for symbol in universe:
            eligible_sessions = [
                trading_date for trading_date in sessions
                if availability_start.get(symbol)
                and trading_date >= availability_start[symbol]
            ]
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
            "history_start_date": symbol_settings[symbol].get("history_start_date"),
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
        "data_contract_version": 2,
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
        "minimum_lookback": minimum_lookback,
        "timezone": "America/New_York",
        "early_close_sessions": [
            item["trading_date"]
            for item in market_sessions
            if item.get("is_early_close")
        ],
        "market_calendar_sha256": _sha256(normalized_sessions),
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
