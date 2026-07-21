from __future__ import annotations

from datetime import date, timedelta

from config import FULL_HISTORY_START_DATE
from database import repository
from services.api_errors import MarketDataError
from services.twelve_data_client import (
    fetch_daily_prices as fetch_twelve_data_daily_prices,
    fetch_daily_prices_batch as fetch_twelve_data_daily_prices_batch,
    fetch_latest_prices_batch as fetch_twelve_data_latest_prices_batch,
)
from services.yahoo_finance_client import (
    fetch_daily_prices as fetch_yahoo_daily_prices,
    fetch_recent_daily_prices_fast as fetch_yahoo_recent_daily_prices_fast,
)


OVERVIEW_DAILY_REFRESH_LOOKBACK_DAYS = 5
TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT = 8


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


def _overview_sync_start_date(symbol: str) -> date:
    configured_start = date.fromisoformat(FULL_HISTORY_START_DATE)
    bounds = repository.get_symbol_date_bounds(symbol)
    if not bounds or not bounds["max_date"]:
        return configured_start
    latest = date.fromisoformat(bounds["max_date"])
    return max(configured_start, latest - timedelta(days=OVERVIEW_DAILY_REFRESH_LOOKBACK_DAYS))


def sync_market_overview_daily_prices() -> dict:
    aliases = repository.list_overview_symbols()
    if not aliases:
        return {
            "ok": True,
            "source": None,
            "start_date": None,
            "end_date": date.today().isoformat(),
            "items": [],
            "updated_rows": 0,
            "message": "行情总览没有需要更新的标的。",
        }

    batch_start_date = min(_overview_sync_start_date(alias["common_symbol"]) for alias in aliases)
    results = {
        alias["common_symbol"]: {
            "symbol": alias["common_symbol"],
            "display_symbol": alias["display_name"],
            "source": None,
            "status": "pending",
            "updated_rows": 0,
            "error": None,
        }
        for alias in aliases
    }

    pending_aliases = _sync_overview_daily_from_yahoo(aliases, batch_start_date, results)
    _sync_overview_daily_from_twelve_data(pending_aliases, batch_start_date, results)

    updated_rows = sum(int(item["updated_rows"] or 0) for item in results.values())
    return {
        "ok": True,
        "source": "mixed" if len({item["source"] for item in results.values() if item["source"]}) > 1 else next(
            (item["source"] for item in results.values() if item["source"]),
            None,
        ),
        "start_date": batch_start_date.isoformat(),
        "end_date": date.today().isoformat(),
        "items": list(results.values()),
        "updated_rows": updated_rows,
    }


def _sync_overview_daily_from_twelve_data(
    aliases: list[dict],
    start_date: date,
    results: dict[str, dict],
) -> list[dict]:
    provider_symbols = [
        alias["twelvedata_symbol"]
        for alias in aliases
        if alias.get("twelvedata_symbol")
    ]
    provider_to_alias = {
        alias["twelvedata_symbol"]: alias
        for alias in aliases
        if alias.get("twelvedata_symbol")
    }
    pending = [
        alias
        for alias in aliases
        if not alias.get("twelvedata_symbol")
    ]

    if not provider_symbols:
        return pending
    if len(provider_symbols) > TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT:
        skipped = provider_symbols[TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT:]
        provider_symbols = provider_symbols[:TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT]
        for provider_symbol in skipped:
            alias = provider_to_alias[provider_symbol]
            results[alias["common_symbol"]].update(
                {
                    "status": "error",
                    "error": "Twelve Data 免费额度不足，已跳过本轮批量补齐。",
                }
            )

    try:
        rows_by_symbol = fetch_twelve_data_daily_prices_batch(provider_symbols, start_date)
    except MarketDataError as exc:
        repository.log_api_request(
            provider="twelvedata",
            status="error",
            symbol=None,
            error_code=exc.code,
            message=f"Batch {','.join(provider_symbols)}: {exc.detail or exc.message}",
        )
        for alias in aliases:
            if alias.get("twelvedata_symbol") and alias not in pending:
                pending.append(alias)
        return pending

    for provider_symbol in provider_symbols:
        alias = provider_to_alias[provider_symbol]
        normalized = alias["common_symbol"]
        rows = rows_by_symbol.get(provider_symbol) or []
        if not rows:
            pending.append(alias)
            repository.log_api_request(
                provider="twelvedata",
                status="error",
                symbol=normalized,
                error_code="EMPTY_DATA",
                message=f"{provider_symbol}: no daily rows in batch response.",
            )
            continue
        repository.upsert_symbol(normalized)
        updated_rows = repository.upsert_daily_prices(normalized, rows)
        results[normalized].update(
            {
                "source": "twelvedata",
                "status": "success",
                "updated_rows": updated_rows,
                "error": None,
            }
        )
        repository.log_api_request(
            provider="twelvedata",
            status="success",
            symbol=normalized,
            message=f"Fetched {updated_rows} overview daily rows from {provider_symbol} since {start_date}.",
        )

    successful = {provider_to_alias[key]["common_symbol"] for key in rows_by_symbol}
    return [
        alias
        for alias in pending
        if alias["common_symbol"] not in successful
    ]


def _sync_overview_daily_from_yahoo(
    aliases: list[dict],
    start_date: date,
    results: dict[str, dict],
) -> list[dict]:
    pending = []
    for alias in aliases:
        normalized = alias["common_symbol"]
        provider_symbol = alias.get("yahoo_symbol")
        if not provider_symbol:
            pending.append(alias)
            continue

        try:
            rows = fetch_yahoo_recent_daily_prices_fast(provider_symbol, start_date=start_date)
            repository.upsert_symbol(normalized)
            updated_rows = repository.upsert_daily_prices(normalized, rows)
            results[normalized].update(
                {
                    "source": "yahoo",
                    "status": "success",
                    "updated_rows": updated_rows,
                    "error": None,
                }
            )
            repository.log_api_request(
                provider="yahoo",
                status="success",
                symbol=normalized,
                message=f"Fetched {updated_rows} overview daily rows from {provider_symbol} since {start_date}.",
            )
        except MarketDataError as exc:
            results[normalized].update(
                {
                    "source": "yahoo",
                    "status": "error",
                    "error": exc.detail or exc.message,
                }
            )
            repository.log_api_request(
                provider="yahoo",
                status="error",
                symbol=normalized,
                error_code=exc.code,
                message=f"{provider_symbol}: {exc.detail or exc.message}",
            )
            pending.append(alias)
    return pending


def refresh_market_overview_latest_prices() -> dict:
    sync_result = sync_market_overview_daily_prices()
    sync_items = {
        item["symbol"]: item
        for item in sync_result.get("items", [])
    }
    overview = repository.list_market_overview()
    items = []
    for item in overview["items"]:
        sync_item = sync_items.get(item["symbol"], {})
        items.append(
            {
                "symbol": item["symbol"],
                "display_symbol": item["display_symbol"],
                "source": sync_item.get("source") or "database",
                "status": sync_item.get("status") or "success",
                "latest_price": item["latest_price"],
                "daily_change": item["daily_change"],
                "daily_change_percent": item["daily_change_percent"],
                "error": sync_item.get("error"),
            }
        )
    return {
        "ok": True,
        "source": sync_result.get("source") or "database",
        "items": items,
        "updated_rows": sync_result.get("updated_rows", 0),
    }


def _refresh_overview_prices_from_yahoo_chart(aliases: list[dict], results: dict[str, dict]) -> list[dict]:
    provider_symbols = [alias["yahoo_symbol"] for alias in aliases if alias.get("yahoo_symbol")]
    provider_to_alias = {
        alias["yahoo_symbol"]: alias
        for alias in aliases
        if alias.get("yahoo_symbol")
    }
    pending = [alias for alias in aliases if not alias.get("yahoo_symbol")]
    if not provider_symbols:
        return pending

    try:
        quotes = fetch_yahoo_latest_chart_prices_batch(provider_symbols)
    except MarketDataError as exc:
        repository.log_api_request(
            provider="yahoo_chart_price",
            status="error",
            symbol=None,
            error_code=exc.code,
            message=f"Batch {','.join(provider_symbols)}: {exc.detail or exc.message}",
        )
        for alias in aliases:
            if alias.get("yahoo_symbol") and alias not in pending:
                pending.append(alias)
        return pending

    for provider_symbol in provider_symbols:
        alias = provider_to_alias[provider_symbol]
        normalized = alias["common_symbol"]
        quote = quotes.get(provider_symbol)
        if not quote:
            pending.append(alias)
            continue
        results[normalized].update(
            {
                "source": "yahoo",
                "status": "success",
                "latest_price": quote["price"],
                "daily_change": quote.get("change"),
                "daily_change_percent": quote.get("change_percent"),
                "error": None,
            }
        )

    successful = {provider_to_alias[key]["common_symbol"] for key in quotes}
    return [
        alias
        for alias in pending
        if alias["common_symbol"] not in successful
    ]


def _refresh_overview_prices_from_twelve_data(aliases: list[dict], results: dict[str, dict]) -> None:
    provider_symbols = [alias["twelvedata_symbol"] for alias in aliases if alias.get("twelvedata_symbol")]
    provider_to_alias = {
        alias["twelvedata_symbol"]: alias
        for alias in aliases
        if alias.get("twelvedata_symbol")
    }
    for alias in aliases:
        if not alias.get("twelvedata_symbol") and results[alias["common_symbol"]]["status"] == "pending":
            results[alias["common_symbol"]].update({"status": "error", "error": "没有可用的数据源代码。"})
    if not provider_symbols:
        return
    if len(provider_symbols) > TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT:
        skipped = provider_symbols[TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT:]
        provider_symbols = provider_symbols[:TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT]
        for provider_symbol in skipped:
            alias = provider_to_alias[provider_symbol]
            if results[alias["common_symbol"]]["status"] == "pending":
                results[alias["common_symbol"]].update(
                    {
                        "source": "twelvedata",
                        "status": "error",
                        "error": "Twelve Data 免费额度不足，已跳过本轮实时价格补充。",
                    }
                )

    try:
        prices = fetch_twelve_data_latest_prices_batch(provider_symbols)
    except MarketDataError as exc:
        repository.log_api_request(
            provider="twelvedata_price",
            status="error",
            symbol=None,
            error_code=exc.code,
            message=f"Batch {','.join(provider_symbols)}: {exc.detail or exc.message}",
        )
        for alias in aliases:
            if alias.get("twelvedata_symbol") and results[alias["common_symbol"]]["status"] == "pending":
                results[alias["common_symbol"]].update(
                    {
                        "source": "twelvedata",
                        "status": "error",
                        "error": exc.detail or exc.message,
                    }
                )
        return

    for provider_symbol in provider_symbols:
        alias = provider_to_alias[provider_symbol]
        normalized = alias["common_symbol"]
        if results[normalized]["status"] == "success":
            continue
        price = prices.get(provider_symbol)
        if not price:
            results[normalized].update(
                {
                    "source": "twelvedata",
                    "status": "error",
                    "error": "Twelve Data 未返回最新价格。",
                }
            )
            continue
        results[normalized].update(
            {
                "source": "twelvedata",
                "status": "success",
                "latest_price": price["price"],
                "error": None,
            }
        )
