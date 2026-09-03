from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from zoneinfo import ZoneInfo

from config import ALPACA_API_KEY, ALPACA_SECRET
from database import repository
from services.alpaca_data_client import (
    fetch_crypto_bars_page,
    fetch_latest_stock_bars,
    fetch_stock_bars,
)
from services.market_data_request_coordinator import (
    PRIORITY_FORMAL_DECISION,
    market_data_request_coordinator,
)
from services.market_context import uses_previous_close_for_historical_intraday
from services.yahoo_finance_client import fetch_latest_chart_prices_batch


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
MAX_SIGNAL_STALENESS_SECONDS = 5 * 60


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class IEXMarketDataHub:
    """Event-time Alpaca data owner for formal realtime decisions.

    There is intentionally no background polling. Each scheduled action takes
    an exact REST snapshot, even when market-overview automatic refresh is off.
    US stocks use IEX and supported crypto symbols use Alpaca Crypto.
    """

    def __init__(self, *, poll_seconds: float = 15.0):
        # Retained as a compatibility attribute for callers/tests. It is not
        # used to schedule network requests.
        self.poll_seconds = max(5.0, float(poll_seconds))
        self._lock = threading.RLock()
        self._symbols: set[str] = set()
        self._latest: dict[str, dict] = {}
        self._last_error: str | None = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def set_symbols(self, symbols: list[str] | tuple[str, ...] | set[str]) -> None:
        with self._lock:
            self._symbols = {
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol).strip() and "/" not in str(symbol)
            }

    def latest(self, symbol: str) -> dict | None:
        with self._lock:
            value = self._latest.get(str(symbol).strip().upper())
            return dict(value) if value else None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def refresh_latest(self, symbols: list[str] | None = None) -> dict[str, dict]:
        with self._lock:
            requested = [
                str(symbol).strip().upper()
                for symbol in (symbols or sorted(self._symbols))
                if str(symbol).strip() and "/" not in str(symbol)
            ]
        if not requested or not ALPACA_API_KEY or not ALPACA_SECRET:
            return {}
        try:
            rows = fetch_latest_stock_bars(requested, feed="iex", timeframe="1Min")
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return {}
        with self._lock:
            self._latest.update(rows)
            self._last_error = None
        return rows

    def event_snapshot(
        self,
        symbols: list[str],
        *,
        trading_date: str,
        event: str,
        now: datetime | None = None,
        include_cumulative_volume: bool = False,
        allow_missing: bool = False,
        market_session: dict | None = None,
        effective_target_at: datetime | None = None,
        previous_session_closes: dict[str, dict] | None = None,
    ) -> dict:
        """Fetch only the Alpaca bars needed by one scheduled event.

        The returned manifest is part of the audit record.  For exact minute
        events, the signal is the last complete minute before the scheduled
        minute and the fill reference is the scheduled minute's open when it
        exists.  CLOSE has no same-close fill reference.
        """
        now = _as_utc(now or datetime.now(UTC))
        current_day = datetime.fromisoformat(trading_date).replace(tzinfo=NEW_YORK)
        open_at = (
            datetime.fromtimestamp(int(market_session["open_minute_utc"]) * 60, tz=UTC)
            if market_session
            else current_day.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)
        )
        close_at = (
            datetime.fromtimestamp(int(market_session["close_minute_utc"]) * 60, tz=UTC)
            if market_session
            else current_day.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(UTC)
        )
        target_at = (
            _as_utc(effective_target_at).astimezone(UTC)
            if effective_target_at is not None
            else close_at if event == "CLOSE"
            else open_at if event == "OPEN"
            else current_day.replace(
                hour=int(event[:2]), minute=int(event[3:]), second=0, microsecond=0
            ).astimezone(UTC)
        )
        end_at = min(max(now, target_at + timedelta(minutes=1)), close_at + timedelta(minutes=2))
        result: dict[str, dict] = {}
        missing: list[str] = []
        for symbol in sorted({str(item).strip().upper() for item in symbols}):
            is_crypto = "/" in symbol
            if uses_previous_close_for_historical_intraday(symbol, "US_EQUITY"):
                try:
                    alias = repository.resolve_symbol_alias(symbol)
                    provider_symbol = alias.get("yahoo_symbol")
                    quotes = fetch_latest_chart_prices_batch([provider_symbol])
                    quote = quotes.get(provider_symbol)
                    if not quote:
                        raise RuntimeError("Yahoo Finance did not return a current price")
                    current_price = float(quote["price"])
                    market_time = quote.get("market_time")
                    signal_time = (
                        datetime.fromtimestamp(float(market_time), tz=UTC)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if isinstance(market_time, (int, float))
                        else str(market_time or now.isoformat().replace("+00:00", "Z"))
                    )
                    result[symbol] = {
                        "signal_price": current_price,
                        "fill_price": None if event == "CLOSE" else current_price,
                        "signal_time": signal_time,
                        "fill_time": None if event == "CLOSE" else signal_time,
                        "latest_minute": None,
                        "daily": {
                            "open": current_price,
                            "high": current_price,
                            "low": current_price,
                            "close": current_price,
                            "volume": 0.0,
                        },
                        "daily_is_complete": False,
                        "cumulative_volume": 0.0 if include_cumulative_volume else None,
                        "source": "yahoo_current_price",
                        "feed": "yahoo",
                        "requested_at": now.isoformat().replace("+00:00", "Z"),
                        "price_fallback": "current_price_without_alpaca_minutes",
                    }
                except Exception as exc:
                    missing.append(f"{symbol}: current-price fallback failed: {exc}")
                continue
            try:
                def fetch_event_pages() -> dict:
                    if is_crypto:
                        minute_page = fetch_crypto_bars_page(
                            symbol,
                            timeframe="1Min",
                            start=(open_at - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                            end=end_at.isoformat().replace("+00:00", "Z"),
                            location="us",
                            limit=1000,
                        )
                        daily_page = {"data": []}
                    else:
                        minute_page = fetch_stock_bars(
                            symbol,
                            timeframe="1Min",
                            start=open_at.isoformat().replace("+00:00", "Z"),
                            end=end_at.isoformat().replace("+00:00", "Z"),
                            feed="iex",
                            limit=1000,
                            max_pages=1,
                        )
                        daily_page = fetch_stock_bars(
                            symbol,
                            timeframe="1Day",
                            # Include prior sessions: the OPEN signal is the last
                            # completed daily close, never the current session's
                            # partial bar.
                            start=(current_day - timedelta(days=10)).date().isoformat(),
                            end=(current_day + timedelta(days=1)).date().isoformat(),
                            feed="iex",
                            limit=10,
                            max_pages=1,
                        )
                    return {
                        "minute_rows": minute_page.get("data", []),
                        "daily_rows": daily_page.get("data", []),
                        "fetched_at": datetime.now(UTC).isoformat().replace(
                            "+00:00", "Z"
                        ),
                    }

                pages = market_data_request_coordinator.run(
                    ("formal-event-bars", symbol, trading_date, event),
                    priority=PRIORITY_FORMAL_DECISION,
                    callback=fetch_event_pages,
                )
                minute_rows = pages["minute_rows"]
                daily_rows = pages["daily_rows"]
            except Exception as exc:
                missing.append(f"{symbol}: {exc}")
                continue
            parsed_minutes = []
            for row in minute_rows:
                try:
                    timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(UTC)
                except (TypeError, ValueError):
                    continue
                parsed_minutes.append({**row, "timestamp": timestamp.isoformat().replace("+00:00", "Z")})
            parsed_minutes.sort(key=lambda item: item["timestamp"])
            parsed_daily = [row for row in daily_rows if str(row.get("timestamp", ""))[:10] == trading_date]
            day_row = parsed_daily[-1] if parsed_daily else (parsed_minutes[-1] if parsed_minutes else None)
            if event == "OPEN":
                previous_daily = [row for row in daily_rows if str(row.get("timestamp", ""))[:10] < trading_date]
                previous_daily.sort(key=lambda item: str(item.get("timestamp", "")))
                previous_session = (previous_session_closes or {}).get(symbol)
                signal_row = (
                    None
                    if is_crypto
                    else (previous_daily[-1] if previous_daily else None)
                )
                fill_row = next(
                    (
                        row for row in parsed_minutes
                        if datetime.fromisoformat(
                            row["timestamp"].replace("Z", "+00:00")
                        ) == open_at
                    ),
                    None,
                )
                if fill_row is None and parsed_minutes:
                    fill_row = next(
                        (
                            row for row in parsed_minutes
                            if datetime.fromisoformat(
                                row["timestamp"].replace("Z", "+00:00")
                            ) >= open_at
                        ),
                        None,
                    )
                signal_price = (
                    float(previous_session["close"])
                    if is_crypto and previous_session
                    else float(signal_row["close"]) if signal_row
                    else None
                )
                fill_price = float(fill_row["open"]) if fill_row else None
                signal_time = (
                    f"{previous_session['date']} CLOSE America/New_York"
                    if is_crypto and previous_session
                    else f"{trading_date} 09:29:59 America/New_York"
                )
            elif event == "CLOSE":
                # A close event must be backed by Alpaca's complete daily bar;
                # a last minute close is not silently substituted.
                signal_row = (
                    next(
                        (
                            row for row in reversed(parsed_minutes)
                            if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) < close_at
                        ),
                        None,
                    )
                    if is_crypto
                    else (parsed_daily[-1] if parsed_daily else None)
                )
                signal_price = float(signal_row["close"]) if signal_row else None
                fill_price = None
                signal_time = str(signal_row.get("timestamp")) if signal_row else None
            else:
                signal_minute = target_at - timedelta(minutes=1)
                fill_minute = target_at
                signal_row = next((row for row in reversed(parsed_minutes) if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= signal_minute), None)
                fill_row = next((row for row in parsed_minutes if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) == fill_minute), None)
                signal_price = float(signal_row["close"]) if signal_row else None
                fill_price = float(fill_row["open"]) if fill_row else None
                signal_time = signal_row.get("timestamp") if signal_row else None
                if signal_row is not None:
                    signal_age = (
                        signal_minute
                        - datetime.fromisoformat(
                            signal_row["timestamp"].replace("Z", "+00:00")
                        )
                    ).total_seconds()
                    if signal_age > MAX_SIGNAL_STALENESS_SECONDS:
                        missing.append(
                            f"{symbol}: {event} 前最新完整分钟已滞后 "
                            f"{int(signal_age // 60)} 分钟"
                        )
                        continue
            if is_crypto and event == "CLOSE" and signal_row is not None:
                expected_minute = close_at - timedelta(minutes=1)
                signal_age = (
                    expected_minute
                    - datetime.fromisoformat(
                        signal_row["timestamp"].replace("Z", "+00:00")
                    )
                ).total_seconds()
                if signal_age > MAX_SIGNAL_STALENESS_SECONDS:
                    missing.append(
                        f"{symbol}: {event} 前最新完整分钟已滞后 "
                        f"{int(signal_age // 60)} 分钟"
                    )
                    continue
            if signal_price is None:
                missing.append(f"{symbol}: 缺少 {trading_date} {event} 的信号行情")
                continue
            cumulative_volume = None
            if include_cumulative_volume:
                boundary = target_at - timedelta(minutes=1)
                cumulative_volume = sum(
                    float(row.get("volume") or 0)
                    for row in parsed_minutes
                    if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= boundary
                )
            result[symbol] = {
                "signal_price": signal_price,
                "fill_price": fill_price,
                "signal_time": signal_time,
                "fill_time": fill_minute.isoformat().replace("+00:00", "Z") if event not in {"OPEN", "CLOSE"} and fill_price is not None else (f"{trading_date} 09:30 America/New_York" if event == "OPEN" and fill_price is not None else None),
                "latest_minute": parsed_minutes[-1] if parsed_minutes else None,
                "daily": day_row,
                "daily_is_complete": bool(parsed_daily),
                "cumulative_volume": cumulative_volume,
                "source": "alpaca_crypto" if is_crypto else "alpaca",
                "feed": "us" if is_crypto else "iex",
                "requested_at": pages["fetched_at"],
            }
        if missing and not allow_missing:
            raise RuntimeError("正式事件行情不完整：" + "；".join(missing[:8]))
        if allow_missing and not result:
            raise RuntimeError("competition 模式没有任何标的取得有效行情：" + "；".join(missing[:8]))
        source_types = {
            value["source"] for value in result.values()
        }
        feeds = {value["feed"] for value in result.values()}
        return {
            "symbols": result,
            "source": (
                next(iter(source_types)) if len(source_types) == 1 else "alpaca_mixed"
            ),
            "feed": next(iter(feeds)) if len(feeds) == 1 else "mixed",
            "timeframe": "1Min",
            "trading_date": trading_date,
            "event": event,
            "missing": missing,
            "requested_at": now.isoformat().replace("+00:00", "Z"),
            "freshness_seconds": {
                symbol: max(0.0, (now - datetime.fromisoformat(value["requested_at"].replace("Z", "+00:00"))).total_seconds())
                for symbol, value in result.items()
            },
        }
