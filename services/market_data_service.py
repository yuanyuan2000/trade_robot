from __future__ import annotations

from datetime import date, timedelta

from config import FULL_HISTORY_START_DATE
from database import repository
from services.api_errors import MarketDataError
from services.twelve_data_client import fetch_daily_prices as fetch_twelve_data_daily_prices
from services.yahoo_finance_client import fetch_daily_prices as fetch_yahoo_daily_prices


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _source_symbol(alias: dict, provider: str) -> str | None:
    if provider == "yahoo":
        return alias.get("yahoo_symbol")
    if provider == "twelvedata":
        return alias.get("twelvedata_symbol")
    return alias.get("common_symbol")


def _fetch_daily_prices_with_fallback(alias: dict, start_date: date) -> tuple[list[dict], str, str]:
    attempts = [
        ("yahoo", _source_symbol(alias, "yahoo"), fetch_yahoo_daily_prices),
        ("twelvedata", _source_symbol(alias, "twelvedata"), fetch_twelve_data_daily_prices),
    ]
    first_error: MarketDataError | None = None

    for provider, provider_symbol, fetcher in attempts:
        if not provider_symbol:
            continue
        try:
            rows = fetcher(provider_symbol, start_date=start_date)
            repository.log_api_request(
                provider=provider,
                status="success",
                symbol=alias["common_symbol"],
                message=f"Fetched {len(rows)} daily rows from {provider_symbol} since {FULL_HISTORY_START_DATE}.",
            )
            return rows, provider, provider_symbol
        except MarketDataError as exc:
            if first_error is None:
                first_error = exc
            repository.log_api_request(
                provider=provider,
                status="error",
                symbol=alias["common_symbol"],
                error_code=exc.code,
                message=f"{provider_symbol}: {exc.detail or exc.message}",
            )

    if first_error:
        raise first_error
    raise ValueError("Symbol is required")


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
    if not normalize_symbol(symbol):
        raise ValueError("Symbol is required")

    alias = repository.resolve_symbol_alias(symbol)
    normalized = alias["common_symbol"]
    display_symbol = alias["display_name"]
    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    if not _has_cached_prices(normalized):
        rows, provider, _provider_symbol = _fetch_daily_prices_with_fallback(alias, start_date)
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(normalized, rows)
        source = provider

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    if not rows:
        return {
            "ok": True,
            "symbol": display_symbol,
            "canonical_symbol": normalized,
            "source": source,
            "warning": f"没有可展示的 {FULL_HISTORY_START_DATE} 以来行情数据。",
            "symbol_settings": repository.get_symbol(normalized),
            "data": [],
            "start_date": start_date.isoformat(),
        }

    return {
        "ok": True,
        "symbol": display_symbol,
        "canonical_symbol": normalized,
        "source": source,
        "warning": None,
        "symbol_settings": repository.get_symbol(normalized),
        "data": rows,
        "start_date": start_date.isoformat(),
    }


def update_full_market_data(symbol: str) -> dict:
    if not normalize_symbol(symbol):
        raise ValueError("Symbol is required")

    alias = repository.resolve_symbol_alias(symbol)
    normalized = alias["common_symbol"]
    display_symbol = alias["display_name"]
    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    if not _has_full_history(normalized, start_date):
        rows, provider, _provider_symbol = _fetch_daily_prices_with_fallback(alias, start_date)
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(normalized, rows)
        source = provider

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    return {
        "ok": True,
        "symbol": display_symbol,
        "canonical_symbol": normalized,
        "source": source,
        "warning": None,
        "symbol_settings": repository.get_symbol(normalized),
        "data": rows,
        "start_date": start_date.isoformat(),
    }
