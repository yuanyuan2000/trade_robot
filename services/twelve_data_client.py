from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests

from config import (
    API_OUTPUT_SIZE,
    FULL_HISTORY_OUTPUT_SIZE,
    MARKET_INTERVAL,
    REQUEST_TIMEOUT_SECONDS,
    TWELVEDATA_API_KEY,
    TWELVEDATA_BASE_URL,
)
from services.api_errors import (
    DataParseError,
    EmptyDataError,
    InvalidApiKeyError,
    InvalidResponseError,
    MissingApiKeyError,
    NetworkError,
    NetworkTimeoutError,
    RateLimitedError,
    SymbolNotFoundError,
)


def _raise_for_twelve_data_error(payload: dict) -> None:
    status = str(payload.get("status", "")).lower()
    code = int(payload.get("code", 0) or 0)
    message = str(payload.get("message") or payload.get("error") or "")
    lowered = message.lower()

    if status != "error" and not message:
        return

    if code in {401, 403} or "api key" in lowered:
        raise InvalidApiKeyError(detail=message)
    if code == 429 or "limit" in lowered or "credits" in lowered:
        raise RateLimitedError(detail=message)
    if "symbol" in lowered or "not found" in lowered or "invalid" in lowered:
        raise SymbolNotFoundError(detail=message)

    raise InvalidResponseError(detail=message or str(payload)[:300])


def fetch_daily_prices(
    symbol: str,
    lookback_days: int | None = None,
    start_date: date | None = None,
) -> list[dict]:
    if not TWELVEDATA_API_KEY:
        raise MissingApiKeyError()

    params = {
        "symbol": symbol,
        "interval": MARKET_INTERVAL,
        "outputsize": FULL_HISTORY_OUTPUT_SIZE if start_date else API_OUTPUT_SIZE,
        "apikey": TWELVEDATA_API_KEY,
    }
    if start_date:
        params["start_date"] = start_date.isoformat()

    try:
        response = requests.get(
            TWELVEDATA_BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            raise RateLimitedError(detail="Twelve Data API credit limit reached.")
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise NetworkTimeoutError(detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise NetworkError(detail=str(exc)) from exc
    except ValueError as exc:
        raise InvalidResponseError(detail="Response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise InvalidResponseError(detail="Response payload is not an object.")

    _raise_for_twelve_data_error(payload)

    values = payload.get("values")
    if not values:
        raise EmptyDataError()
    if not isinstance(values, list):
        raise InvalidResponseError(detail="'values' is not a list.")

    try:
        df = pd.DataFrame(values)
        if "volume" not in df.columns:
            df["volume"] = 0
        df = df.rename(
            columns={
                "datetime": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            }
        )
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise DataParseError(detail=f"Missing columns: {missing}")

        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["date"] = pd.to_datetime(df["date"]).dt.date
        if start_date:
            df = df[df["date"] >= start_date]
        elif lookback_days is not None:
            start = date.today() - timedelta(days=lookback_days)
            df = df[df["date"] >= start]
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.sort_values("date")
    except DataParseError:
        raise
    except Exception as exc:
        raise DataParseError(detail=str(exc)) from exc

    rows = [
        {
            "date": row.date.isoformat(),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume) if pd.notna(row.volume) else 0,
        }
        for row in df.itertuples(index=False)
    ]
    if not rows:
        raise EmptyDataError()
    return rows


def fetch_daily_prices_batch(symbols: list[str], start_date: date) -> dict[str, list[dict]]:
    clean_symbols = [symbol for symbol in symbols if symbol]
    if not clean_symbols:
        return {}
    if not TWELVEDATA_API_KEY:
        raise MissingApiKeyError()

    params = {
        "symbol": ",".join(clean_symbols),
        "interval": MARKET_INTERVAL,
        "outputsize": FULL_HISTORY_OUTPUT_SIZE,
        "start_date": start_date.isoformat(),
        "apikey": TWELVEDATA_API_KEY,
    }

    try:
        response = requests.get(
            TWELVEDATA_BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            raise RateLimitedError(detail="Twelve Data API credit limit reached.")
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise NetworkTimeoutError(detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise NetworkError(detail=str(exc)) from exc
    except ValueError as exc:
        raise InvalidResponseError(detail="Response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise InvalidResponseError(detail="Response payload is not an object.")

    _raise_for_twelve_data_error(payload)

    if len(clean_symbols) == 1 and "values" in payload:
        return {clean_symbols[0]: _parse_time_series_payload(payload, start_date)}

    result: dict[str, list[dict]] = {}
    errors: list[str] = []
    for symbol in clean_symbols:
        item = payload.get(symbol)
        if not isinstance(item, dict):
            errors.append(f"{symbol}: missing payload")
            continue
        try:
            result[symbol] = _parse_time_series_payload(item, start_date)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")

    if not result and errors:
        raise EmptyDataError(detail="; ".join(errors[:5]))
    return result


def fetch_latest_prices_batch(symbols: list[str]) -> dict[str, dict]:
    clean_symbols = [symbol for symbol in symbols if symbol]
    if not clean_symbols:
        return {}
    if not TWELVEDATA_API_KEY:
        raise MissingApiKeyError()

    params = {
        "symbol": ",".join(clean_symbols),
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        response = requests.get(
            "https://api.twelvedata.com/price",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            raise RateLimitedError(detail="Twelve Data API credit limit reached.")
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise NetworkTimeoutError(detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise NetworkError(detail=str(exc)) from exc
    except ValueError as exc:
        raise InvalidResponseError(detail="Response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise InvalidResponseError(detail="Response payload is not an object.")

    _raise_for_twelve_data_error(payload)

    if len(clean_symbols) == 1 and "price" in payload:
        return {clean_symbols[0]: {"price": float(payload["price"])}}

    result: dict[str, dict] = {}
    for symbol in clean_symbols:
        item = payload.get(symbol)
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if price in {None, ""}:
            continue
        result[symbol] = {"price": float(price)}
    return result


def _parse_time_series_payload(payload: dict, start_date: date | None = None) -> list[dict]:
    _raise_for_twelve_data_error(payload)

    values = payload.get("values")
    if not values:
        raise EmptyDataError()
    if not isinstance(values, list):
        raise InvalidResponseError(detail="'values' is not a list.")

    try:
        df = pd.DataFrame(values)
        if "volume" not in df.columns:
            df["volume"] = 0
        df = df.rename(
            columns={
                "datetime": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            }
        )
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise DataParseError(detail=f"Missing columns: {missing}")

        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["date"] = pd.to_datetime(df["date"]).dt.date
        if start_date:
            df = df[df["date"] >= start_date]
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.sort_values("date")
    except DataParseError:
        raise
    except Exception as exc:
        raise DataParseError(detail=str(exc)) from exc

    rows = [
        {
            "date": row.date.isoformat(),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume) if pd.notna(row.volume) else 0,
        }
        for row in df.itertuples(index=False)
    ]
    if not rows:
        raise EmptyDataError()
    return rows
