from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Iterable

US_EQUITY_MARKET = {
    "type": "US_EQUITY",
    "calendar": "XNYS",
    "timezone": "America/New_York",
}
SUPPORTED_MARKET_TYPES = {"US_EQUITY"}
US_SESSION_SERIES = "US_EQUITY_SESSION"
NATIVE_DAILY_SERIES = "NATIVE_DAILY"


def normalize_market_config(value: dict | str | None) -> dict:
    from services.backtest.errors import BacktestValidationError

    if value is None:
        return deepcopy(US_EQUITY_MARKET)
    if isinstance(value, str):
        market_type = value.strip().upper()
    elif isinstance(value, dict):
        market_type = str(value.get("type") or "US_EQUITY").strip().upper()
    else:
        raise BacktestValidationError("策略市场配置必须是对象。")
    if market_type not in SUPPORTED_MARKET_TYPES:
        raise BacktestValidationError(
            f"暂不支持策略市场类型 {market_type or '空值'}；当前仅支持美股。"
        )
    # Calendar and timezone are owned by the market type.  Callers cannot
    # create internally inconsistent combinations.
    return deepcopy(US_EQUITY_MARKET)


def market_sessions(
    start_date: str,
    end_date: str,
    market: dict | str | None = None,
) -> list[dict]:
    from services.backtest.errors import BacktestValidationError
    from services.backtest.market_calendar import ensure_market_sessions

    normalized = normalize_market_config(market)
    if normalized["type"] == "US_EQUITY":
        return ensure_market_sessions(start_date, end_date)
    raise BacktestValidationError(f"没有 {normalized['type']} 的交易日历实现。")


def market_session_dates(
    start_date: str,
    end_date: str,
    market: dict | str | None = None,
) -> set[str]:
    return {
        str(item["trading_date"])
        for item in market_sessions(start_date, end_date, market)
    }


def filter_rows_for_market(
    rows: Iterable[dict],
    market: dict | str | None = None,
) -> list[dict]:
    values = [dict(row) for row in rows]
    if not values:
        return []
    allowed = market_session_dates(
        min(str(row["date"])[:10] for row in values),
        max(str(row["date"])[:10] for row in values),
        market,
    )
    return [row for row in values if str(row["date"])[:10] in allowed]


def annotate_us_market_sessions(rows: Iterable[dict]) -> list[dict]:
    values = [dict(row) for row in rows]
    if not values:
        return []
    allowed = market_session_dates(
        min(str(row["date"])[:10] for row in values),
        max(str(row["date"])[:10] for row in values),
        US_EQUITY_MARKET,
    )
    return [
        {
            **row,
            "is_us_market_session": str(row["date"])[:10] in allowed,
        }
        for row in values
    ]


def strategy_daily_series(symbol: str, market: dict | str | None = None) -> str:
    normalized = normalize_market_config(market)
    if normalized["type"] == "US_EQUITY" and "/" in str(symbol):
        return US_SESSION_SERIES
    return NATIVE_DAILY_SERIES


def calendar_contract(market: dict | str | None = None) -> dict:
    return normalize_market_config(market)
