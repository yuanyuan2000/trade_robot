from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import requests

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET,
    ALPACA_TRADING_BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
)
from database import intraday_repository
from services.backtest.errors import BacktestDataError


NEW_YORK = ZoneInfo("America/New_York")


def _parse_clock(value: str) -> time:
    normalized = str(value).strip()
    try:
        return time.fromisoformat(normalized)
    except ValueError as exc:
        raise BacktestDataError(f"交易日历时间格式异常：{value}。") from exc


def fetch_market_sessions(start_date: str, end_date: str) -> list[dict]:
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        raise BacktestDataError(
            "缺少 Alpaca 凭据，无法核验美股交易日历；为避免错误事件时间，回测已停止。"
        )
    try:
        response = requests.get(
            f"{ALPACA_TRADING_BASE_URL}/calendar",
            params={"start": start_date, "end": end_date},
            headers={
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=max(REQUEST_TIMEOUT_SECONDS, 30),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BacktestDataError(
            "无法从 Alpaca 核验交易日、夏令时和提前收盘；回测已停止。",
            detail=str(exc),
        ) from exc
    if not isinstance(payload, list):
        raise BacktestDataError("Alpaca 交易日历响应格式异常。")
    sessions = []
    for item in payload:
        trading_date = date.fromisoformat(str(item["date"]))
        open_time = _parse_clock(item["open"])
        close_time = _parse_clock(item["close"])
        open_at = datetime.combine(trading_date, open_time, tzinfo=NEW_YORK)
        close_at = datetime.combine(trading_date, close_time, tzinfo=NEW_YORK)
        sessions.append(
            {
                "trading_date": trading_date.isoformat(),
                "open_minute_utc": int(
                    open_at.astimezone(timezone.utc).timestamp()
                ) // 60,
                "close_minute_utc": int(
                    close_at.astimezone(timezone.utc).timestamp()
                ) // 60,
                "is_early_close": close_time < time(16, 0),
            }
        )
    return sessions


def ensure_market_sessions(start_date: str, end_date: str) -> list[dict]:
    coverage = intraday_repository.get_market_calendar_coverage()
    covered = (
        coverage
        and coverage["status"] == "success"
        and coverage["coverage_start"] <= start_date
        and coverage["coverage_end"] >= end_date
    )
    if not covered:
        # Coverage is represented by one continuous interval. Refetch the
        # entire union when extending it, otherwise two disjoint successful
        # fetches could make MIN/MAX falsely claim an unverified middle range.
        fetch_start = start_date
        fetch_end = end_date
        if coverage and coverage["status"] == "success":
            fetch_start = min(fetch_start, coverage["coverage_start"])
            fetch_end = max(fetch_end, coverage["coverage_end"])
        try:
            sessions = fetch_market_sessions(fetch_start, fetch_end)
            intraday_repository.upsert_market_sessions(
                sessions,
                coverage_start=fetch_start,
                coverage_end=fetch_end,
            )
        except Exception as exc:
            intraday_repository.mark_market_calendar_sync_error(
                coverage_start=fetch_start,
                coverage_end=fetch_end,
                error=str(exc),
            )
            raise
    sessions = intraday_repository.get_market_sessions(start_date, end_date)
    if not sessions:
        raise BacktestDataError("所选日期范围没有美股交易日。")
    return sessions
