from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import threading
from typing import Callable

from config import FULL_HISTORY_START_DATE
from database import intraday_repository, repository
from services.alpaca_data_client import (
    fetch_crypto_bars_page,
    fetch_stock_bars_page,
)


ALPACA_HISTORY_DELAY_MINUTES = 16
ALPACA_IMPORT_PAGE_SIZE = 10_000
MAX_IMPORT_NETWORK_WORKERS = 2
_symbol_locks_guard = threading.Lock()
_symbol_locks: dict[str, threading.Lock] = {}


def default_import_end() -> str:
    value = datetime.now(timezone.utc) - timedelta(
        minutes=ALPACA_HISTORY_DELAY_MINUTES
    )
    return value.replace(second=0, microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _symbol_lock(symbol: str) -> threading.Lock:
    normalized = symbol.strip().upper()
    with _symbol_locks_guard:
        return _symbol_locks.setdefault(normalized, threading.Lock())


def import_symbol_history(
    symbol: str,
    *,
    start: str = FULL_HISTORY_START_DATE,
    end: str | None = None,
    feed: str = "sip",
    max_pages: int | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Download and persist a symbol page by page, resuming from saved state."""
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    end_value = end or default_import_end()
    is_crypto = normalized == "BTC/USD"
    effective_feed = "us" if is_crypto and feed == "sip" else feed

    with _symbol_lock(normalized):
        job = intraday_repository.create_or_resume_import_job(
            normalized,
            start,
            end_value,
            feed=effective_feed,
        )
        end_value = job.get("end_at") or end_value
        if job.get("status") == "completed":
            return {
                "ok": True,
                "symbol": normalized,
                "start": start,
                "end": end_value,
                "complete": True,
                "pages_this_run": 0,
                "job": job,
                "sync_state": intraday_repository.get_sync_state(normalized),
                "fingerprint_months_updated": [],
                "cached_completed_job": True,
            }
        next_page_token = job.get("next_page_token")
        pages_this_run = 0
        touched_months: set[str] = set()
        job = intraday_repository.update_import_job(
            job["id"],
            status="running",
            next_page_token=next_page_token,
        )
        try:
            while True:
                page = (
                    fetch_crypto_bars_page(
                        normalized,
                        timeframe="1Min",
                        start=start,
                        end=end_value,
                        location=effective_feed,
                        limit=ALPACA_IMPORT_PAGE_SIZE,
                        page_token=next_page_token,
                    )
                    if is_crypto
                    else fetch_stock_bars_page(
                        normalized,
                        timeframe="1Min",
                        start=start,
                        end=end_value,
                        feed=effective_feed,
                        limit=ALPACA_IMPORT_PAGE_SIZE,
                        page_token=next_page_token,
                    )
                )
                rows = page["data"]
                written = intraday_repository.upsert_minute_bars(
                    normalized,
                    rows,
                    asset_class="crypto" if is_crypto else "us_equity",
                )
                if rows:
                    first_date = min(str(row["timestamp"])[:10] for row in rows)
                    repository.mark_symbol_history_start(
                        normalized,
                        first_date,
                        source="alpaca_crypto" if is_crypto else "alpaca",
                        asset_class="crypto" if is_crypto else "us_equity",
                        quantity_step=0.0001 if is_crypto else None,
                    )
                for row in rows:
                    touched_months.add(str(row["timestamp"])[:7])
                next_page_token = page["next_page_token"]
                pages_this_run += 1
                status = "completed" if not next_page_token else "running"
                job = intraday_repository.update_import_job(
                    job["id"],
                    status=status,
                    next_page_token=next_page_token,
                    pages_added=1,
                    rows_added=written,
                )
                if progress:
                    page_first_at = rows[0]["timestamp"] if rows else None
                    page_last_at = rows[-1]["timestamp"] if rows else end_value
                    progress(
                        {
                            "symbol": normalized,
                            "job": job,
                            "page_rows": len(rows),
                            "page_first_at": page_first_at,
                            "page_last_at": page_last_at,
                            "rate_limit": page["rate_limit"],
                        }
                    )
                if not next_page_token:
                    break
                if max_pages is not None and pages_this_run >= max_pages:
                    job = intraday_repository.update_import_job(
                        job["id"],
                        status="paused",
                        next_page_token=next_page_token,
                    )
                    break

            for year_month in sorted(touched_months):
                intraday_repository.recompute_monthly_fingerprint(
                    normalized,
                    year_month,
                )
            sync_state = intraday_repository.mark_sync_result(
                normalized,
                "success" if not next_page_token else "paused",
            )
            return {
                "ok": True,
                "symbol": normalized,
                "start": start,
                "end": end_value,
                "complete": next_page_token is None,
                "pages_this_run": pages_this_run,
                "job": job,
                "sync_state": sync_state,
                "fingerprint_months_updated": sorted(touched_months),
            }
        except Exception as exc:
            intraday_repository.update_import_job(
                job["id"],
                status="error",
                next_page_token=next_page_token,
                error=str(exc),
            )
            intraday_repository.mark_sync_result(
                normalized,
                "error",
                error=str(exc),
            )
            raise


def import_symbols_history(
    symbols: list[str],
    *,
    start: str = FULL_HISTORY_START_DATE,
    end: str | None = None,
    feed: str = "sip",
    max_pages: int | None = None,
    max_workers: int = MAX_IMPORT_NETWORK_WORKERS,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Import symbols through a small worker pool sharing the global limiter."""
    normalized_symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    if not normalized_symbols:
        raise ValueError("At least one symbol is required")
    worker_count = max(1, min(int(max_workers), MAX_IMPORT_NETWORK_WORKERS))
    shared_end = end or default_import_end()
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                import_symbol_history,
                symbol,
                start=start,
                end=shared_end,
                feed=feed,
                max_pages=max_pages,
                progress=progress,
            ): symbol
            for symbol in normalized_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                results[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)
    return {
        "ok": not errors,
        "start": start,
        "end": shared_end,
        "workers": worker_count,
        "results": results,
        "errors": errors,
    }
