from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from contextlib import contextmanager
import socket
import time as time_module
from urllib.parse import quote

import requests

from config import FULL_HISTORY_OUTPUT_SIZE, REQUEST_TIMEOUT_SECONDS
from services.api_errors import (
    DataParseError,
    EmptyDataError,
    InvalidResponseError,
    MarketDataError,
    NetworkError,
    NetworkTimeoutError,
    RateLimitedError,
    SymbolNotFoundError,
)


YAHOO_CHART_BASE_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart",
    "https://query2.finance.yahoo.com/v8/finance/chart",
]
YAHOO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


@contextmanager
def force_ipv4_dns():
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_getaddrinfo(*args, **kwargs):
        return [
            item
            for item in original_getaddrinfo(*args, **kwargs)
            if item[0] == socket.AF_INET
        ]

    socket.getaddrinfo = ipv4_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def fetch_daily_prices(symbol: str, start_date: date | None = None) -> list[dict]:
    if not symbol:
        raise SymbolNotFoundError(detail="Yahoo Finance symbol is empty.")

    if start_date:
        rows_by_date: dict[str, dict] = {}
        chunk_start = start_date
        today = date.today()
        while chunk_start <= today:
            chunk_end = min(chunk_start + timedelta(days=370), today)
            for row in _fetch_daily_price_chunk(symbol, chunk_start, chunk_end):
                rows_by_date[row["date"]] = row
            chunk_start = chunk_end + timedelta(days=1)

        rows = [rows_by_date[key] for key in sorted(rows_by_date)]
        if not rows:
            raise EmptyDataError(detail="Yahoo Finance returned no usable OHLC rows.")
        return rows

    params = {
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
        "range": f"{FULL_HISTORY_OUTPUT_SIZE}d",
    }
    return _fetch_daily_price_payload(symbol, params)


def fetch_recent_daily_prices_fast(symbol: str, start_date: date) -> list[dict]:
    if not symbol:
        raise SymbolNotFoundError(detail="Yahoo Finance symbol is empty.")

    today = date.today()
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone.utc)
    params = {
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
        "period1": str(int(start_at.timestamp())),
        "period2": str(int(end_at.timestamp())),
    }
    last_error: MarketDataError | None = None
    for base_url in YAHOO_CHART_BASE_URLS:
        try:
            payload = _fetch_chart_payload_from_url(
                symbol,
                params,
                base_url,
                timeout=(2, 4),
            )
            rows = _parse_daily_price_payload(payload)
            return [row for row in rows if start_date <= date.fromisoformat(row["date"]) <= today]
        except MarketDataError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise InvalidResponseError(detail="Yahoo Finance chart request failed.")


def fetch_latest_quotes_batch(symbols: list[str]) -> dict[str, dict]:
    clean_symbols = [symbol for symbol in symbols if symbol]
    if not clean_symbols:
        return {}

    last_error: MarketDataError | None = None
    params = {"symbols": ",".join(clean_symbols)}
    for attempt in range(3):
        try:
            with force_ipv4_dns():
                response = requests.get(
                    "https://query1.finance.yahoo.com/v7/finance/quote",
                    params=params,
                    headers={"User-Agent": YAHOO_USER_AGENT},
                    timeout=(5, REQUEST_TIMEOUT_SECONDS),
                )
            if response.status_code == 429:
                raise RateLimitedError(detail="Yahoo Finance rate limited the request.")
            response.raise_for_status()
            payload = response.json()
            return _parse_quote_payload(payload)
        except RateLimitedError as exc:
            last_error = exc
        except requests.Timeout as exc:
            last_error = NetworkTimeoutError(detail=str(exc))
        except requests.RequestException as exc:
            last_error = NetworkError(detail=str(exc))
        except ValueError as exc:
            last_error = InvalidResponseError(detail="Yahoo Finance quote response is not valid JSON.")
        if attempt < 2:
            time_module.sleep(0.8 * (attempt + 1))

    if last_error:
        raise last_error
    raise InvalidResponseError(detail="Yahoo Finance quote request failed.")


def _parse_quote_payload(payload: dict) -> dict[str, dict]:
    quote_response = payload.get("quoteResponse") if isinstance(payload, dict) else None
    if not isinstance(quote_response, dict):
        raise InvalidResponseError(detail="Yahoo Finance quote payload is missing.")

    error = quote_response.get("error")
    if error:
        description = error.get("description") if isinstance(error, dict) else str(error)
        raise InvalidResponseError(detail=description)

    rows = quote_response.get("result")
    if not isinstance(rows, list):
        raise InvalidResponseError(detail="Yahoo Finance quote result has an unexpected shape.")

    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        price = row.get("regularMarketPrice") or row.get("postMarketPrice") or row.get("preMarketPrice")
        if not symbol or price is None:
            continue
        result[symbol] = {
            "price": float(price),
            "change": row.get("regularMarketChange"),
            "change_percent": row.get("regularMarketChangePercent"),
            "market_time": row.get("regularMarketTime"),
            "market_state": row.get("marketState"),
        }
    return result


def fetch_latest_chart_prices_batch(symbols: list[str]) -> dict[str, dict]:
    clean_symbols = [symbol for symbol in symbols if symbol]
    if not clean_symbols:
        return {}

    result: dict[str, dict] = {}
    errors: list[MarketDataError] = []
    for symbol in clean_symbols:
        try:
            quote = _fetch_latest_chart_price(symbol)
            if quote:
                result[symbol] = quote
        except MarketDataError as exc:
            errors.append(exc)
        time_module.sleep(0.12)

    if not result and errors:
        raise errors[0]
    return result


def _fetch_latest_chart_price(symbol: str) -> dict | None:
    params = {
        "range": "5d",
        "interval": "1d",
        "includePrePost": "true",
    }
    payload = _fetch_chart_payload(symbol, params)
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise InvalidResponseError(detail="Yahoo Finance chart payload is missing.")

    error = chart.get("error")
    if error:
        description = error.get("description") if isinstance(error, dict) else str(error)
        raise SymbolNotFoundError(detail=description)

    results = chart.get("result")
    if not results:
        raise EmptyDataError(detail="Yahoo Finance returned no chart results.")

    try:
        item = results[0]
        meta = item.get("meta") or {}
        timestamps = item.get("timestamp") or []
        quote_values = (item.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote_values.get("close") or []
        previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    except (AttributeError, IndexError) as exc:
        raise InvalidResponseError(detail="Yahoo Finance chart payload has an unexpected shape.") from exc

    latest_price = None
    latest_timestamp = None
    for index in range(len(closes) - 1, -1, -1):
        close_price = closes[index]
        if close_price is None:
            continue
        latest_price = float(close_price)
        latest_timestamp = timestamps[index] if index < len(timestamps) else None
        break

    if latest_price is None:
        regular_price = meta.get("regularMarketPrice")
        if regular_price is None:
            return None
        latest_price = float(regular_price)

    daily_change = None
    daily_change_percent = None
    if previous_close not in {None, 0}:
        daily_change = latest_price - float(previous_close)
        daily_change_percent = daily_change / float(previous_close) * 100

    return {
        "price": latest_price,
        "change": daily_change,
        "change_percent": daily_change_percent,
        "market_time": latest_timestamp or meta.get("regularMarketTime"),
        "market_state": meta.get("marketState"),
    }


def _fetch_daily_price_chunk(symbol: str, start_date: date, end_date: date) -> list[dict]:
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    params = {
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
        "period1": str(int(start_at.timestamp())),
        "period2": str(int(end_at.timestamp())),
    }
    rows = _fetch_daily_price_payload(symbol, params)
    return [row for row in rows if start_date <= date.fromisoformat(row["date"]) <= end_date]


def _fetch_daily_price_payload(symbol: str, params: dict) -> list[dict]:
    last_error: MarketDataError | None = None
    for attempt in range(3):
        for base_url in YAHOO_CHART_BASE_URLS:
            try:
                payload = _fetch_chart_payload_from_url(symbol, params, base_url)
                return _parse_daily_price_payload(payload)
            except (NetworkTimeoutError, NetworkError, RateLimitedError) as exc:
                last_error = exc
                continue
        if attempt < 2:
            time_module.sleep(0.8 * (attempt + 1))

    if last_error:
        raise last_error
    raise InvalidResponseError(detail="Yahoo Finance chart request failed.")


def _fetch_chart_payload(symbol: str, params: dict) -> dict:
    last_error: MarketDataError | None = None
    for attempt in range(3):
        for base_url in YAHOO_CHART_BASE_URLS:
            try:
                return _fetch_chart_payload_from_url(symbol, params, base_url)
            except (NetworkTimeoutError, NetworkError, RateLimitedError) as exc:
                last_error = exc
                continue
        if attempt < 2:
            time_module.sleep(0.8 * (attempt + 1))

    if last_error:
        raise last_error
    raise InvalidResponseError(detail="Yahoo Finance chart request failed.")


def _fetch_chart_payload_from_url(
    symbol: str,
    params: dict,
    base_url: str,
    timeout: tuple[int, int] | None = None,
) -> dict:
    request_timeout = timeout or (5, REQUEST_TIMEOUT_SECONDS)
    try:
        with force_ipv4_dns():
            response = requests.get(
                f"{base_url}/{quote(symbol, safe='')}",
                params=params,
                headers={"User-Agent": YAHOO_USER_AGENT},
                timeout=request_timeout,
            )
        if response.status_code == 429:
            raise RateLimitedError(detail="Yahoo Finance rate limited the request.")
        response.raise_for_status()
        payload = response.json()
    except RateLimitedError:
        raise
    except requests.Timeout as exc:
        raise NetworkTimeoutError(detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise NetworkError(detail=str(exc)) from exc
    except ValueError as exc:
        raise InvalidResponseError(detail="Yahoo Finance response is not valid JSON.") from exc

    return payload


def _parse_daily_price_payload(payload: dict) -> list[dict]:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise InvalidResponseError(detail="Yahoo Finance chart payload is missing.")

    error = chart.get("error")
    if error:
        description = error.get("description") if isinstance(error, dict) else str(error)
        raise SymbolNotFoundError(detail=description)

    results = chart.get("result")
    if not results:
        raise EmptyDataError(detail="Yahoo Finance returned no chart results.")

    try:
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote_values = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens = quote_values.get("open") or []
        highs = quote_values.get("high") or []
        lows = quote_values.get("low") or []
        closes = quote_values.get("close") or []
        volumes = quote_values.get("volume") or []
    except (AttributeError, IndexError) as exc:
        raise InvalidResponseError(detail="Yahoo Finance chart payload has an unexpected shape.") from exc

    rows = []
    for index, timestamp in enumerate(timestamps):
        try:
            open_price = opens[index]
            high_price = highs[index]
            low_price = lows[index]
            close_price = closes[index]
        except IndexError as exc:
            raise DataParseError(detail="Yahoo Finance OHLC arrays have inconsistent lengths.") from exc

        if None in {open_price, high_price, low_price, close_price}:
            continue

        volume = volumes[index] if index < len(volumes) and volumes[index] is not None else 0
        rows.append(
            {
                "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume),
            }
        )

    if not rows:
        raise EmptyDataError(detail="Yahoo Finance returned no usable OHLC rows.")

    return rows
