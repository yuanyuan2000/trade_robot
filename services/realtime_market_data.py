from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time
from zoneinfo import ZoneInfo

from config import ALPACA_API_KEY, ALPACA_SECRET
from services.alpaca_data_client import fetch_latest_stock_bars, fetch_stock_bars


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _minute_timestamp(value: datetime) -> datetime:
    return _as_utc(value).astimezone(UTC).replace(second=0, microsecond=0)


class IEXMarketDataHub:
    """Shared IEX data owner for realtime decision tasks.

    The free Alpaca plan is deliberately pinned to IEX.  A background polling
    loop keeps a cheap latest-bar cache warm; event snapshots use a bounded
    historical request so the evaluator can prove which complete minute was
    used.  The scheduled REST snapshot remains the source of truth for
    decisions, while the cache can later be fed by Alpaca's websocket stream.
    """

    def __init__(self, *, poll_seconds: float = 15.0):
        self.poll_seconds = max(5.0, float(poll_seconds))
        self._lock = threading.RLock()
        self._symbols: set[str] = set()
        self._latest: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="realtime-iex", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

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

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.refresh_latest()

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
    ) -> dict:
        """Fetch only the IEX bars needed by one scheduled event.

        The returned manifest is part of the audit record.  For exact minute
        events, the signal is the last complete minute before the scheduled
        minute and the fill reference is the scheduled minute's open when it
        exists.  CLOSE has no same-close fill reference.
        """
        now = _as_utc(now or datetime.now(UTC))
        current_day = datetime.fromisoformat(trading_date).replace(tzinfo=NEW_YORK)
        open_at = current_day.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)
        close_at = current_day.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(UTC)
        target_at = close_at if event == "CLOSE" else open_at if event == "OPEN" else current_day.replace(
            hour=int(event[:2]), minute=int(event[3:]), second=0, microsecond=0
        ).astimezone(UTC)
        end_at = min(max(now, target_at + timedelta(minutes=1)), close_at + timedelta(minutes=2))
        result: dict[str, dict] = {}
        missing: list[str] = []
        for symbol in sorted({str(item).strip().upper() for item in symbols}):
            # BTC/USD and other slash symbols belong to Alpaca's crypto
            # endpoint, not the free stock IEX feed.  Do not turn this into a
            # misleading generic response-format error.
            if "/" in symbol:
                missing.append(f"{symbol}: IEX 股票行情不支持该加密货币代码")
                continue
            try:
                minute_page = fetch_stock_bars(
                    symbol,
                    timeframe="1Min",
                    start=open_at.isoformat().replace("+00:00", "Z"),
                    end=end_at.isoformat().replace("+00:00", "Z"),
                    feed="iex",
                    limit=1000,
                    max_pages=1,
                )
                minute_rows = minute_page.get("data", [])
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
                daily_rows = daily_page.get("data", [])
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
                signal_row = previous_daily[-1] if previous_daily else None
                fill_row = next((row for row in parsed_minutes if row["timestamp"] == open_at), None)
                if fill_row is None and parsed_minutes:
                    fill_row = parsed_minutes[0]
                signal_price = float(signal_row["close"]) if signal_row else None
                fill_price = float(fill_row["open"]) if fill_row else None
                signal_time = f"{trading_date} 09:29:59 America/New_York"
            elif event == "CLOSE":
                # A close event must be backed by Alpaca's complete daily bar;
                # a last minute close is not silently substituted.
                signal_row = parsed_daily[-1] if parsed_daily else None
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
                "source": "alpaca",
                "feed": "iex",
                "requested_at": now.isoformat().replace("+00:00", "Z"),
            }
        if missing and not allow_missing:
            raise RuntimeError("IEX 行情不完整：" + "；".join(missing[:8]))
        if allow_missing and not result:
            raise RuntimeError("competition 模式没有任何标的取得有效 IEX 行情：" + "；".join(missing[:8]))
        return {
            "symbols": result,
            "source": "alpaca",
            "feed": "iex",
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
