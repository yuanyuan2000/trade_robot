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
                return _fetch_daily_price_payload_from_url(symbol, params, base_url)
            except (NetworkTimeoutError, NetworkError, RateLimitedError) as exc:
                last_error = exc
                continue
        if attempt < 2:
            time_module.sleep(0.8 * (attempt + 1))

    if last_error:
        raise last_error
    raise InvalidResponseError(detail="Yahoo Finance chart request failed.")


def _fetch_daily_price_payload_from_url(symbol: str, params: dict, base_url: str) -> list[dict]:
    try:
        with force_ipv4_dns():
            response = requests.get(
                f"{base_url}/{quote(symbol, safe='')}",
                params=params,
                headers={"User-Agent": YAHOO_USER_AGENT},
                timeout=(5, REQUEST_TIMEOUT_SECONDS),
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
