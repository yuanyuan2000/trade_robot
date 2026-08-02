from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from config import FULL_HISTORY_START_DATE
from database import repository
from services.api_errors import MarketDataError
from services.alpaca_data_client import fetch_stock_bars
from services.intraday_bar_service import (
    derive_daily_prices_from_minutes,
    refresh_alpaca_capability,
)
from services.intraday_import_service import import_symbol_history
from database import intraday_repository
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
INTRADAY_REFRESH_LOOKBACK_DAYS = 5
TWELVEDATA_FREE_BATCH_SYMBOL_LIMIT = 8


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _source_symbol(alias: dict, provider: str) -> str | None:
    if provider == "yahoo":
        return alias.get("yahoo_symbol")
    if provider == "twelvedata":
        return alias.get("twelvedata_symbol")
    return alias.get("common_symbol")


def _ensure_alpaca_capability(symbol: str) -> dict:
    settings = repository.get_symbol(symbol)
    if settings.get("alpaca_supported") is None:
        return refresh_alpaca_capability(symbol)["symbol_settings"]
    return settings


def _fetch_alpaca_daily_prices(symbol: str, start_date: date) -> list[dict]:
    end = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    payload = fetch_stock_bars(
        symbol,
        timeframe="1Day",
        start=start_date.isoformat(),
        end=end,
        feed="sip",
        limit=10_000,
        max_pages=2,
    )
    return [
        {
            "date": row["timestamp"][:10],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "source_provider": "alpaca",
            "source_timeframe": "1Day",
            "is_complete": True,
        }
        for row in payload["data"]
    ]


def _fetch_daily_prices_with_fallback(alias: dict, start_date: date) -> tuple[list[dict], str, str]:
    normalized = alias["common_symbol"]
    attempts = []
    try:
        capability = _ensure_alpaca_capability(normalized)
    except MarketDataError as exc:
        repository.log_api_request(
            provider="alpaca",
            status="error",
            symbol=normalized,
            error_code=exc.code,
            message=exc.detail or exc.message,
        )
        capability = {"alpaca_supported": False}
    if capability.get("alpaca_supported"):
        attempts.append(
            (
                "alpaca",
                capability.get("alpaca_symbol") or normalized,
                _fetch_alpaca_daily_prices,
            )
        )
    attempts.extend([
        ("yahoo", _source_symbol(alias, "yahoo"), fetch_yahoo_daily_prices),
        ("twelvedata", _source_symbol(alias, "twelvedata"), fetch_twelve_data_daily_prices),
    ])
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
                message=(
                    f"Fetched {len(rows)} daily rows from {provider_symbol} "
                    f"since {start_date.isoformat()}."
                ),
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


def _has_history_start(symbol: str, start_date: date) -> bool:
    coverage = repository.get_symbol_history_coverage(symbol)
    if not coverage or not coverage["min_date"]:
        return False
    return (
        date.fromisoformat(coverage["min_date"])
        <= first_required_data_date(start_date)
    )


def _has_cached_prices(symbol: str) -> bool:
    return repository.get_symbol_date_bounds(symbol) is not None


def _intraday_incremental_start(latest_complete_at: str) -> str:
    latest = datetime.fromisoformat(
        latest_complete_at.replace("Z", "+00:00")
    )
    return (
        latest - timedelta(days=INTRADAY_REFRESH_LOOKBACK_DAYS)
    ).date().isoformat()


def _has_initialized_intraday_history(symbol: str, sync_state: dict) -> bool:
    earliest_at = sync_state.get("earliest_minute_at")
    latest_complete_at = sync_state.get("latest_complete_minute_at")
    if (
        int(sync_state.get("row_count") or 0) <= 0
        or not earliest_at
        or not latest_complete_at
    ):
        return False
    earliest_date = datetime.fromisoformat(
        earliest_at.replace("Z", "+00:00")
    ).date()
    required_start = first_required_data_date(date.fromisoformat(FULL_HISTORY_START_DATE))
    try:
        settings = repository.get_symbol(symbol)
    except Exception:
        settings = {}
    history_start = settings.get("history_start_date")
    if settings.get("history_start_verified") and history_start:
        required_start = max(required_start, date.fromisoformat(history_start))
    return earliest_date <= required_start


def first_required_data_date(start_date: date) -> date:
    if start_date == date(2020, 1, 1):
        return date(2020, 1, 2)

    current = start_date
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def get_market_data(
    symbol: str,
    *,
    include_intraday: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    if not normalize_symbol(symbol):
        raise ValueError("Symbol is required")

    alias = repository.resolve_symbol_alias(symbol)
    normalized = alias["common_symbol"]
    if include_intraday:
        update_kwargs = {"initialize_intraday": True}
        if progress_callback:
            update_kwargs["progress_callback"] = progress_callback
        return update_full_market_data(normalized, **update_kwargs)
    display_symbol = alias["display_name"]
    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    if not _has_cached_prices(normalized):
        update_kwargs = {"initialize_intraday": False}
        if progress_callback:
            update_kwargs["progress_callback"] = progress_callback
        return update_full_market_data(normalized, **update_kwargs)

    if progress_callback:
        progress_callback(
            {
                "stage": "completed",
                "progress": 1.0,
                "current_date": None,
                "message": "本地行情数据读取完成",
            }
        )

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    if not rows:
        return {
            "ok": True,
            "symbol": display_symbol,
            "canonical_symbol": normalized,
            "source": source,
            "warning": f"没有可展示的 {FULL_HISTORY_START_DATE} 以来行情数据。",
            "symbol_settings": repository.get_symbol(normalized),
            "intraday_sync": intraday_repository.get_sync_state(normalized),
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
        "intraday_sync": intraday_repository.get_sync_state(normalized),
        "data": rows,
        "start_date": start_date.isoformat(),
    }


def update_full_market_data(
    symbol: str,
    *,
    initialize_intraday: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    if not normalize_symbol(symbol):
        raise ValueError("Symbol is required")

    alias = repository.resolve_symbol_alias(symbol)
    normalized = alias["common_symbol"]
    display_symbol = alias["display_name"]
    start_date = date.fromisoformat(FULL_HISTORY_START_DATE)
    source = "database"

    def emit(**payload) -> None:
        if progress_callback:
            progress_callback(payload)

    emit(
        stage="checking",
        progress=0.02,
        current_date=None,
        message=f"正在检查 {display_symbol} 的数据覆盖范围",
    )
    try:
        capability = _ensure_alpaca_capability(normalized)
    except MarketDataError as exc:
        repository.log_api_request(
            provider="alpaca",
            status="error",
            symbol=normalized,
            error_code=exc.code,
            message=exc.detail or exc.message,
        )
        capability = {"alpaca_supported": False}

    sync_state = intraday_repository.get_sync_state(normalized)
    intraday_ready = _has_initialized_intraday_history(normalized, sync_state)
    if capability.get("alpaca_supported") and initialize_intraday:
        if intraday_ready:
            start_value = _intraday_incremental_start(
                sync_state["latest_complete_minute_at"]
            )
            derive_start = start_value
        else:
            start_value = FULL_HISTORY_START_DATE
            derive_start = None
        progress_start = date.fromisoformat(start_value[:10])
        progress_end = date.today()
        total_days = max(1, (progress_end - progress_start).days)

        def import_progress(item: dict) -> None:
            current_date = str(item.get("page_last_at") or "")[:10] or None
            completed_ratio = 0.0
            if current_date:
                try:
                    completed_ratio = min(
                        1.0,
                        max(
                            0.0,
                            (date.fromisoformat(current_date) - progress_start).days
                            / total_days,
                        ),
                    )
                except ValueError:
                    completed_ratio = 0.0
            emit(
                stage="intraday",
                progress=0.05 + completed_ratio * 0.85,
                current_date=current_date,
                pages=int(item["job"].get("pages_fetched") or 0),
                rows=int(item["job"].get("rows_written") or 0),
                message=(
                    f"分钟数据已更新至 {current_date}"
                    if current_date
                    else "正在更新分钟数据"
                ),
            )

        import_kwargs = {"start": start_value}
        if progress_callback:
            import_kwargs["progress"] = import_progress
        import_result = import_symbol_history(normalized, **import_kwargs)
        emit(
            stage="deriving_daily",
            progress=0.93,
            current_date=(
                str(import_result.get("end") or "")[:10] or None
            ),
            message="分钟数据下载完成，正在重建日线",
        )
        derived = derive_daily_prices_from_minutes(
            normalized,
            start_at=derive_start,
        )
        rows = repository.get_daily_prices(normalized, start_date.isoformat())
        emit(
            stage="completed",
            progress=1.0,
            current_date=rows[-1]["date"] if rows else None,
            message="行情数据更新完成",
        )
        return {
            "ok": True,
            "symbol": display_symbol,
            "canonical_symbol": normalized,
            "source": "alpaca",
            "warning": None,
            "symbol_settings": repository.get_symbol(normalized),
            "intraday_sync": import_result["sync_state"],
            "derived_daily": derived,
            "data": rows,
            "start_date": start_date.isoformat(),
        }

    recent_start = _overview_sync_start_date(normalized)
    emit(
        stage="daily",
        progress=0.15,
        current_date=recent_start.isoformat(),
        message=f"正在更新 {recent_start.isoformat()} 起的日线数据",
    )
    if capability.get("alpaca_supported"):
        rows = _fetch_alpaca_daily_prices(
            capability.get("alpaca_symbol") or normalized,
            recent_start,
        )
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(
            normalized,
            rows,
            source_provider="alpaca",
            source_timeframe="1Day",
        )
        source = "alpaca"
    else:
        rows, provider, _provider_symbol = _fetch_daily_prices_with_fallback(
            alias,
            recent_start,
        )
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(
            normalized,
            rows,
            source_provider=provider,
            source_timeframe="1Day",
        )
        source = provider

    if recent_start > start_date and not _has_history_start(normalized, start_date):
        rows, provider, _provider_symbol = _fetch_daily_prices_with_fallback(alias, start_date)
        repository.upsert_symbol(normalized)
        repository.upsert_daily_prices(
            normalized,
            rows,
            source_provider=provider,
            source_timeframe="1Day",
        )
        source = provider

    rows = repository.get_daily_prices(normalized, start_date.isoformat())
    settings = repository.get_symbol(normalized)
    emit(
        stage="completed",
        progress=1.0,
        current_date=rows[-1]["date"] if rows else None,
        message="日线数据更新完成",
    )
    return {
        "ok": True,
        "symbol": display_symbol,
        "canonical_symbol": normalized,
        "source": source,
        "warning": (
            settings.get("alpaca_error")
            if settings.get("alpaca_supported") is False
            else None
        ),
        "symbol_settings": settings,
        "intraday_sync": intraday_repository.get_sync_state(normalized),
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

    pending_aliases = _sync_overview_daily_from_alpaca(
        aliases,
        batch_start_date,
        results,
    )
    pending_aliases = _sync_overview_daily_from_yahoo(
        pending_aliases,
        batch_start_date,
        results,
    )
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


def _sync_overview_daily_from_alpaca(
    aliases: list[dict],
    start_date: date,
    results: dict[str, dict],
) -> list[dict]:
    pending = []
    for alias in aliases:
        normalized = alias["common_symbol"]
        try:
            capability = _ensure_alpaca_capability(normalized)
            if not capability.get("alpaca_supported"):
                pending.append(alias)
                continue
            sync_state = intraday_repository.get_sync_state(normalized)
            intraday_ready = _has_initialized_intraday_history(normalized, sync_state)
            if intraday_ready:
                start_value = _intraday_incremental_start(
                    sync_state["latest_complete_minute_at"]
                )
                import_result = import_symbol_history(
                    normalized,
                    start=start_value,
                )
                derived = derive_daily_prices_from_minutes(
                    normalized,
                    start_at=start_value,
                )
                updated_rows = derived["updated_rows"]
                source = "alpaca-minute"
                sync_status = import_result["sync_state"]["status"]
            else:
                rows = _fetch_alpaca_daily_prices(
                    capability.get("alpaca_symbol") or normalized,
                    start_date,
                )
                repository.upsert_symbol(normalized)
                updated_rows = repository.upsert_daily_prices(
                    normalized,
                    rows,
                    source_provider="alpaca",
                    source_timeframe="1Day",
                )
                source = "alpaca"
                sync_status = sync_state.get("status")
            results[normalized].update(
                {
                    "source": source,
                    "status": "success",
                    "updated_rows": updated_rows,
                    "error": None,
                    "intraday_status": sync_status,
                }
            )
        except MarketDataError as exc:
            repository.log_api_request(
                provider="alpaca",
                status="error",
                symbol=normalized,
                error_code=exc.code,
                message=exc.detail or exc.message,
            )
            pending.append(alias)
    return pending


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
