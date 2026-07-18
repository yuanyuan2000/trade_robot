from __future__ import annotations

from datetime import date, timedelta

from config import FULL_HISTORY_START_DATE
from database import repository
from services.api_errors import MarketDataError
from services.twelve_data_client import fetch_daily_prices


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _has_full_history(symbol: str, start_date: date) -> bool:
    coverage = repository.get_symbol_history_coverage(symbol)
    if not coverage or not coverage["max_complete_date"]:
        return False

    max_complete_date = date.fromisoformat(coverage["max_complete_date"])
    min_date = date.fromisoformat(coverage["min_date"])
    required_start = first_required_data_date(start_date)
    required_latest = latest_required_data_date(date.today())
    return min_date <= required_start and max_complete_date >= required_latest


def _has_cached_prices(symbol: str) -> bool:
    return repository.get_symbol_date_bounds(symbol) is not None


def first_required_data_date(start_date: date) -> date:
    if start_date == date(2020, 1, 1):
        return date(2020, 1, 2)

    current = start_date
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def latest_required_data_date(today: date) -> date:
    if today.weekday() == 5:
        return today - timedelta(days=1)
    if today.weekday() == 6:
        return today - timedelta(days=2)
    return today


def get_market_data(symbol: str) -> dict:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValueError("Symbol is required")

    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    if not _has_cached_prices(normalized):
        try:
            rows = fetch_daily_prices(normalized, start_date=start_date)
            repository.upsert_symbol(normalized)
            repository.upsert_daily_prices(normalized, rows)
            repository.log_api_request(
                provider="twelvedata",
                status="success",
                symbol=normalized,
                message=f"Fetched {len(rows)} daily rows from {FULL_HISTORY_START_DATE}.",
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
            raise

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    if not rows:
        return {
            "ok": True,
            "symbol": normalized,
            "source": source,
            "warning": f"没有可展示的 {FULL_HISTORY_START_DATE} 以来行情数据。",
            "symbol_settings": repository.get_symbol(normalized),
            "data": [],
            "start_date": start_date.isoformat(),
        }

    return {
        "ok": True,
        "symbol": normalized,
        "source": source,
        "warning": None,
        "symbol_settings": repository.get_symbol(normalized),
        "data": rows,
        "start_date": start_date.isoformat(),
    }


def update_full_market_data(symbol: str) -> dict:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValueError("Symbol is required")

    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    if not _has_full_history(normalized, start_date):
        rows = fetch_daily_prices(normalized, start_date=start_date)
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(normalized, rows)
        repository.log_api_request(
            provider="twelvedata",
            status="success",
            symbol=normalized,
            message=f"Updated full history with {len(rows)} daily rows from {FULL_HISTORY_START_DATE}.",
        )
        source = "api"

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    return {
        "ok": True,
        "symbol": normalized,
        "source": source,
        "warning": None,
        "symbol_settings": repository.get_symbol(normalized),
        "data": rows,
        "start_date": start_date.isoformat(),
    }
