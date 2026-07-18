from __future__ import annotations

from datetime import date, timedelta

from config import LOOKBACK_DAYS
from database import repository
from services.api_errors import MarketDataError
from services.twelve_data_client import fetch_daily_prices


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _has_fresh_year(symbol: str, start_date: date) -> bool:
    bounds = repository.get_symbol_date_bounds(symbol)
    if not bounds:
        return False

    max_date = date.fromisoformat(bounds["max_date"])
    min_date = date.fromisoformat(bounds["min_date"])
    return min_date <= start_date and max_date >= date.today() - timedelta(days=3)


def get_market_data(symbol: str) -> dict:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValueError("Symbol is required")

    start_date = date.today() - timedelta(days=LOOKBACK_DAYS)
    source = "database"

    if not _has_fresh_year(normalized, start_date):
        try:
            rows = fetch_daily_prices(normalized, LOOKBACK_DAYS)
            repository.upsert_symbol(normalized)
            repository.upsert_daily_prices(normalized, rows)
            repository.log_api_request(
                provider="twelvedata",
                status="success",
                symbol=normalized,
                message=f"Fetched {len(rows)} daily rows.",
            )
            source = "api"
        except MarketDataError as exc:
            repository.log_api_request(
                provider="twelvedata",
                status="error",
                symbol=normalized,
                error_code=exc.code,
                message=exc.detail or exc.message,
            )
            cached_rows = repository.get_daily_prices(normalized, start_date.isoformat())
            if cached_rows:
                return {
                    "ok": True,
                    "symbol": normalized,
                    "source": "stale_cache",
                    "warning": "行情服务暂时不可用，当前展示的是本地缓存数据。",
                    "data": cached_rows,
                }
            raise

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    if not rows:
        return {
            "ok": True,
            "symbol": normalized,
            "source": source,
            "warning": "没有可展示的最近一年行情数据。",
            "data": [],
        }

    return {
        "ok": True,
        "symbol": normalized,
        "source": source,
        "warning": None,
        "data": rows,
    }
