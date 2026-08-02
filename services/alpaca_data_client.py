from __future__ import annotations

from datetime import datetime
import re
import threading
import time

import requests

from config import (
    ALPACA_API_KEY,
    ALPACA_DATA_BASE_URL,
    ALPACA_SAFE_REQUESTS_PER_MINUTE,
    ALPACA_SECRET,
    ALPACA_TRADING_BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
)
from services.api_errors import (
    DataParseError,
    EmptyDataError,
    InvalidAlpacaCredentialsError,
    InvalidResponseError,
    MissingAlpacaCredentialsError,
    NetworkError,
    NetworkTimeoutError,
    RateLimitedError,
    SymbolNotFoundError,
)


ALPACA_MAX_PAGE_SIZE = 10_000
ALPACA_MAX_PAGES_PER_CALL = 180
ALPACA_RATE_LIMIT_RESERVE = 5
_TIMEFRAME_PATTERN = re.compile(
    r"^(?:[1-9]|[1-5][0-9])Min$"
    r"|^(?:[1-9]|1[0-9]|2[0-3])Hour$"
    r"|^1Day$|^1Week$|^(?:1|2|3|4|6|12)Month$",
    re.IGNORECASE,
)
_request_lock = threading.Lock()
_session_local = threading.local()
_last_request_started = 0.0
_rate_limit_state: dict[str, int | None] = {
    "limit": None,
    "remaining": None,
    "reset": None,
}


def _normalize_timeframe(timeframe: str) -> str:
    value = timeframe.strip()
    if not _TIMEFRAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Unsupported timeframe. Use values such as 1Min, 5Min, 1Hour, or 1Day."
        )
    lowered = value.lower()
    if lowered.endswith("min"):
        return f"{int(value[:-3])}Min"
    if lowered.endswith("hour"):
        return f"{int(value[:-4])}Hour"
    if lowered.endswith("day"):
        return "1Day"
    if lowered.endswith("week"):
        return "1Week"
    return f"{int(value[:-5])}Month"


def _validate_datetime(value: str, field_name: str) -> str:
    clean_value = value.strip()
    if not clean_value:
        raise ValueError(f"{field_name} is required")
    try:
        datetime.fromisoformat(clean_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 date or datetime") from exc
    return clean_value


def _wait_for_request_slot() -> None:
    """Serialize requests and stay below the free-plan limit in this process."""
    global _last_request_started

    minimum_interval = 60.0 / max(1, ALPACA_SAFE_REQUESTS_PER_MINUTE)
    with _request_lock:
        now = time.monotonic()
        wait_seconds = minimum_interval - (now - _last_request_started)

        remaining = _rate_limit_state["remaining"]
        reset_at = _rate_limit_state["reset"]
        if (
            remaining is not None
            and remaining <= ALPACA_RATE_LIMIT_RESERVE
            and reset_at is not None
        ):
            wait_seconds = max(wait_seconds, float(reset_at) - time.time() + 0.25)

        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 60.0))
        _last_request_started = time.monotonic()


def _http_session() -> requests.Session:
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2,
            pool_maxsize=2,
            max_retries=0,
        )
        session.mount("https://", adapter)
        _session_local.session = session
    return session


def _parse_rate_limit_headers(headers) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for output_key, header_name in (
        ("limit", "X-RateLimit-Limit"),
        ("remaining", "X-RateLimit-Remaining"),
        ("reset", "X-RateLimit-Reset"),
    ):
        raw_value = headers.get(header_name)
        try:
            result[output_key] = int(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            result[output_key] = None
    return result


def _update_rate_limit_state(headers) -> dict[str, int | None]:
    current = _parse_rate_limit_headers(headers)
    _rate_limit_state.update(current)
    return current


def _response_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:300]
    return str(payload)[:300]


def _raise_for_alpaca_error(response: requests.Response) -> None:
    if response.status_code < 400:
        return

    detail = _response_error_detail(response)
    if response.status_code == 401:
        raise InvalidAlpacaCredentialsError(detail=detail)
    if response.status_code == 404:
        raise SymbolNotFoundError(detail=detail)
    if response.status_code == 429:
        raise RateLimitedError(
            message="Alpaca 请求频率已达上限，请稍后再试。",
            detail=detail,
        )
    if response.status_code in {400, 403, 422}:
        raise InvalidResponseError(detail=detail)

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError(detail=detail or str(exc)) from exc


def _request_bars_page(
    params: dict,
    *,
    url: str | None = None,
) -> tuple[dict, dict[str, int | None]]:
    response = None
    last_error = None
    for attempt in range(4):
        _wait_for_request_slot()
        try:
            response = _http_session().get(
                url or f"{ALPACA_DATA_BASE_URL}/stocks/bars",
                params=params,
                headers={
                    "APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
                timeout=max(REQUEST_TIMEOUT_SECONDS, 30),
            )
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
        except requests.RequestException as exc:
            raise NetworkError(detail=str(exc)) from exc
    if response is None:
        if isinstance(last_error, requests.Timeout):
            raise NetworkTimeoutError(detail=str(last_error)) from last_error
        raise NetworkError(detail=str(last_error)) from last_error

    rate_limit = _update_rate_limit_state(response.headers)
    _raise_for_alpaca_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise InvalidResponseError(detail="Alpaca response is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidResponseError(detail="Alpaca response payload is not an object.")
    return payload, rate_limit


def fetch_crypto_bars_page(
    symbol: str,
    *,
    timeframe: str = "1Min",
    start: str = "2021-01-01",
    end: str | None = None,
    location: str = "us",
    limit: int = ALPACA_MAX_PAGE_SIZE,
    page_token: str | None = None,
) -> dict:
    """Fetch one resumable page from Alpaca's crypto bars endpoint."""
    normalized_symbol = symbol.strip().upper()
    if normalized_symbol != "BTC/USD":
        raise ValueError("当前仅支持 BTC/USD 加密行情。")
    normalized_timeframe = _normalize_timeframe(timeframe)
    start_value = _validate_datetime(start, "start")
    end_value = _validate_datetime(end, "end") if end else None
    normalized_location = location.strip().lower()
    if normalized_location not in {"us", "us-1"}:
        raise ValueError("crypto location must be us or us-1")
    if not 1 <= limit <= ALPACA_MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {ALPACA_MAX_PAGE_SIZE}")
    params = {
        "symbols": normalized_symbol,
        "timeframe": normalized_timeframe,
        "start": start_value,
        "sort": "asc",
        "limit": limit,
    }
    if end_value:
        params["end"] = end_value
    if page_token:
        params["page_token"] = page_token
    crypto_base = ALPACA_DATA_BASE_URL.rsplit("/v2", 1)[0]
    payload, rate_limit = _request_bars_page(
        params,
        url=f"{crypto_base}/v1beta3/crypto/{normalized_location}/bars",
    )
    return {
        "source": "alpaca_crypto",
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "feed": normalized_location,
        "start": start_value,
        "end": end_value,
        "data": _parse_bars(payload, normalized_symbol),
        "next_page_token": payload.get("next_page_token"),
        "rate_limit": rate_limit,
    }


def fetch_asset(symbol: str) -> dict:
    """Resolve whether Alpaca recognizes an active US equity or ETF."""
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        raise MissingAlpacaCredentialsError()
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    _wait_for_request_slot()
    try:
        response = _http_session().get(
            f"{ALPACA_TRADING_BASE_URL}/assets/{normalized}",
            headers={
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise NetworkTimeoutError(detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise NetworkError(detail=str(exc)) from exc
    rate_limit = _update_rate_limit_state(response.headers)
    if response.status_code == 404:
        return {
            "symbol": normalized,
            "supported": False,
            "reason": "Alpaca 未收录该标的。",
            "rate_limit": rate_limit,
        }
    _raise_for_alpaca_error(response)
    try:
        asset = response.json()
    except ValueError as exc:
        raise InvalidResponseError(detail="Alpaca asset response is not valid JSON.") from exc
    supported = (
        asset.get("class") == "us_equity"
        and asset.get("status") == "active"
    )
    return {
        "symbol": str(asset.get("symbol") or normalized).upper(),
        "asset_id": asset.get("id"),
        "supported": supported,
        "reason": None if supported else "该标的不是 Alpaca 支持的活跃美股或 ETF。",
        "asset_class": asset.get("class"),
        "exchange": asset.get("exchange"),
        "name": asset.get("name"),
        "tradable": bool(asset.get("tradable")),
        "rate_limit": rate_limit,
    }


def _parse_bars(payload: dict, symbol: str) -> list[dict]:
    bars_by_symbol = payload.get("bars")
    if not isinstance(bars_by_symbol, dict):
        raise InvalidResponseError(detail="Alpaca response is missing the bars object.")
    raw_bars = bars_by_symbol.get(symbol)
    if raw_bars is None:
        return []
    if not isinstance(raw_bars, list):
        raise InvalidResponseError(detail="Alpaca bars payload has an unexpected shape.")

    rows = []
    try:
        for bar in raw_bars:
            rows.append(
                {
                    "timestamp": str(bar["t"]),
                    "open": float(bar["o"]),
                    "high": float(bar["h"]),
                    "low": float(bar["l"]),
                    "close": float(bar["c"]),
                    "volume": float(bar.get("v") or 0),
                    "trade_count": int(bar["n"]) if bar.get("n") is not None else None,
                    "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataParseError(detail=f"Invalid Alpaca bar: {exc}") from exc
    return rows


def fetch_stock_bars_page(
    symbol: str,
    *,
    timeframe: str = "1Min",
    start: str = "2020-01-01",
    end: str | None = None,
    feed: str = "sip",
    limit: int = ALPACA_MAX_PAGE_SIZE,
    page_token: str | None = None,
) -> dict:
    """Fetch exactly one Alpaca page for resumable storage imports."""
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        raise MissingAlpacaCredentialsError()

    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("Symbol is required")
    normalized_timeframe = _normalize_timeframe(timeframe)
    start_value = _validate_datetime(start, "start")
    end_value = _validate_datetime(end, "end") if end else None
    normalized_feed = feed.strip().lower()
    if normalized_feed not in {"iex", "sip"}:
        raise ValueError("feed must be either iex or sip")
    if not 1 <= limit <= ALPACA_MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {ALPACA_MAX_PAGE_SIZE}")

    params = {
        "symbols": normalized_symbol,
        "timeframe": normalized_timeframe,
        "start": start_value,
        "feed": normalized_feed,
        "adjustment": "raw",
        "sort": "asc",
        "limit": limit,
    }
    if end_value:
        params["end"] = end_value
    if page_token:
        params["page_token"] = page_token

    payload, rate_limit = _request_bars_page(params)
    return {
        "source": "alpaca",
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "feed": normalized_feed,
        "start": start_value,
        "end": end_value,
        "data": _parse_bars(payload, normalized_symbol),
        "next_page_token": payload.get("next_page_token"),
        "rate_limit": rate_limit,
    }


def fetch_stock_bars(
    symbol: str,
    *,
    timeframe: str = "1Min",
    start: str = "2020-01-01",
    end: str | None = None,
    feed: str = "sip",
    limit: int = ALPACA_MAX_PAGE_SIZE,
    max_pages: int = 1,
) -> dict:
    """Fetch historical US stock bars without writing them to the local database.

    ``max_pages`` defaults to one so the diagnostic HTTP endpoint cannot
    accidentally download years of minute bars. Historical SIP data is
    available to the tested Basic account when ``end`` is at least 15 minutes
    old; recent SIP data still requires a paid subscription.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        raise MissingAlpacaCredentialsError()

    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("Symbol is required")
    normalized_timeframe = _normalize_timeframe(timeframe)
    start_value = _validate_datetime(start, "start")
    end_value = _validate_datetime(end, "end") if end else None
    normalized_feed = feed.strip().lower()
    if normalized_feed not in {"iex", "sip"}:
        raise ValueError("feed must be either iex or sip")
    if not 1 <= limit <= ALPACA_MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {ALPACA_MAX_PAGE_SIZE}")
    if not 1 <= max_pages <= ALPACA_MAX_PAGES_PER_CALL:
        raise ValueError(
            f"max_pages must be between 1 and {ALPACA_MAX_PAGES_PER_CALL}"
        )

    rows: list[dict] = []
    pages_fetched = 0
    next_page_token: str | None = None
    rate_limit: dict[str, int | None] = {
        "limit": None,
        "remaining": None,
        "reset": None,
    }
    while pages_fetched < max_pages:
        page = fetch_stock_bars_page(
            normalized_symbol,
            timeframe=normalized_timeframe,
            start=start_value,
            end=end_value,
            feed=normalized_feed,
            limit=limit,
            page_token=next_page_token,
        )
        rows.extend(page["data"])
        pages_fetched += 1
        next_page_token = page["next_page_token"]
        rate_limit = page["rate_limit"]
        if not next_page_token:
            break

    if not rows:
        raise EmptyDataError(detail="Alpaca returned no bars for the requested range.")

    return {
        "ok": True,
        "source": "alpaca",
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "feed": normalized_feed,
        "start": start_value,
        "end": end_value,
        "data_count": len(rows),
        "data": rows,
        "pagination": {
            "pages_fetched": pages_fetched,
            "complete": next_page_token is None,
            "next_page_token": next_page_token,
        },
        "rate_limit": rate_limit,
    }
