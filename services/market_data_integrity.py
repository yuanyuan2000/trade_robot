from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from database import repository
from services.backtest.market_calendar import ensure_market_sessions
from services.market_context import filter_rows_for_market, normalize_market_config


NEW_YORK = ZoneInfo("America/New_York")


class MarketDataIntegrityError(RuntimeError):
    code = "HISTORY_STALE"


def required_completed_sessions(
    trading_date: str,
    count: int,
) -> list[str]:
    """Return the exact completed US sessions preceding ``trading_date``."""
    required = max(1, int(count))
    cutoff = date.fromisoformat(str(trading_date)[:10])
    lookback_days = max(45, required * 3 + 20)
    start = cutoff - timedelta(days=lookback_days)
    sessions = ensure_market_sessions(start.isoformat(), cutoff.isoformat())
    values = [
        str(item["trading_date"])
        for item in sessions
        if str(item["trading_date"]) < cutoff.isoformat()
    ]
    if len(values) < required:
        raise MarketDataIntegrityError(
            f"交易日历仅提供 {len(values)} 个历史交易日，策略需要 {required} 个。"
        )
    return values[-required:]


def latest_completed_session_dates(
    count: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    # The current US-equity daily bar is trusted only after the existing
    # provider-delay boundary. Before then it belongs to the live observation.
    cutoff = current.date()
    if current.time() >= time(16, 20):
        cutoff += timedelta(days=1)
    return required_completed_sessions(cutoff.isoformat(), count)


def assess_daily_history(
    symbol: str,
    expected_dates: list[str],
    *,
    respect_verified_start: bool = False,
    market: dict | str | None = None,
) -> dict:
    normalized = str(symbol).strip().upper()
    market_config = normalize_market_config(market)
    rows = repository.get_strategy_daily_prices(
        normalized,
        market_config["type"],
        include_metadata=True,
    )
    expected = list(expected_dates)
    if respect_verified_start:
        try:
            settings = repository.get_symbol(normalized)
        except Exception:
            settings = {}
        history_start = settings.get("daily_history_start_date")
        if settings.get("daily_history_start_verified") and history_start:
            expected = [value for value in expected if value >= str(history_start)]

    by_date = {str(row["date"]): dict(row) for row in rows}
    missing = [value for value in expected if value not in by_date]
    incomplete = [
        value for value in expected
        if value in by_date and not bool(by_date[value].get("is_complete", True))
    ]
    usable = [
        by_date[value]
        for value in expected
        if value in by_date and value not in incomplete
    ]
    latest = usable[-1] if usable else None
    sources = sorted({str(row.get("source_provider") or "unknown") for row in usable})
    timeframes = sorted({str(row.get("source_timeframe") or "unknown") for row in usable})
    bases = sorted({str(row.get("price_basis") or "unknown") for row in usable})
    updated_values = [str(row.get("updated_at") or "") for row in usable if row.get("updated_at")]
    fingerprint_payload = [
        [
            row.get("date"), row.get("open"), row.get("high"),
            row.get("low"), row.get("close"), row.get("is_complete", True),
        ]
        for row in usable
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    problems = [*missing, *incomplete]
    return {
        "symbol": normalized,
        "complete": not problems and len(usable) == len(expected),
        "required_session_count": len(expected_dates),
        "effective_required_session_count": len(expected),
        "expected_first_date": expected[0] if expected else None,
        "expected_last_date": expected[-1] if expected else None,
        "latest_complete_date": str(latest["date"]) if latest else None,
        "latest_complete_close": float(latest["close"]) if latest else None,
        "missing_sessions": missing,
        "incomplete_sessions": incomplete,
        "repair_start_date": min(problems) if problems else None,
        "source_providers": sources,
        "source_timeframes": timeframes,
        "price_bases": bases,
        "data_updated_at": max(updated_values) if updated_values else None,
        "fingerprint": fingerprint,
        "snapshot_id": f"{normalized}:{expected[-1] if expected else 'none'}:{fingerprint}",
    }


def frozen_daily_rows(
    symbol: str,
    *,
    before_date: str,
    market: dict | str | None = None,
) -> list[dict[str, Any]]:
    market_config = normalize_market_config(market)
    rows = repository.get_strategy_daily_prices(
        str(symbol).strip().upper(),
        market_config["type"],
        include_metadata=True,
    )
    completed = [
        dict(row) for row in rows
        if str(row["date"]) < str(before_date)
        and bool(row.get("is_complete", True))
    ]
    return filter_rows_for_market(completed, market_config)
