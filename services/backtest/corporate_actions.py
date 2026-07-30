from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import requests

from config import (
    ALPACA_API_KEY,
    ALPACA_DATA_BASE_URL,
    ALPACA_SECRET,
    REQUEST_TIMEOUT_SECONDS,
)
from database import backtest_repository
from services.backtest.errors import BacktestDataError


CORPORATE_ACTIONS_URL = (
    ALPACA_DATA_BASE_URL.rsplit("/v2", 1)[0] + "/v1/corporate-actions"
)
SUPPORTED_ACTIONS = {"forward_split", "reverse_split", "cash_dividend"}
MAX_CACHE_AGE = timedelta(hours=24)


def _coverage_is_fresh(state: dict | None) -> bool:
    if not state or not state.get("synced_at"):
        return False
    try:
        synced_at = datetime.fromisoformat(str(state["synced_at"]))
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - synced_at <= MAX_CACHE_AGE


def _event_symbol(group: str, event: dict) -> str | None:
    for key in (
        "symbol",
        "source_symbol",
        "old_symbol",
        "acquiree_symbol",
    ):
        if event.get(key):
            return str(event[key]).upper()
    return None


def _singular_action_type(group: str) -> str:
    mapping = {
        "forward_splits": "forward_split",
        "reverse_splits": "reverse_split",
        "cash_dividends": "cash_dividend",
        "stock_dividends": "stock_dividend",
        "unit_splits": "unit_split",
        "spin_offs": "spin_off",
        "cash_mergers": "cash_merger",
        "stock_mergers": "stock_merger",
        "stock_and_cash_mergers": "stock_and_cash_merger",
        "redemptions": "redemption",
        "name_changes": "name_change",
        "worthless_removals": "worthless_removal",
        "rights_distributions": "rights_distribution",
        "reorganizations": "reorganization",
        "partial_calls": "partial_call",
    }
    return mapping.get(group, group.rstrip("s"))


def _normalize_payload(payload: dict) -> tuple[list[dict], str | None]:
    container = payload.get("corporate_actions")
    if not isinstance(container, dict):
        container = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    actions: list[dict] = []
    for group, values in container.items():
        if group in {"next_page_token", "page_token"} or not isinstance(values, list):
            continue
        action_type = _singular_action_type(group)
        for event in values:
            if not isinstance(event, dict):
                continue
            symbol = _event_symbol(group, event)
            process_date = event.get("process_date")
            if not symbol or not process_date:
                continue
            digest = hashlib.sha256(
                json.dumps(
                    {"group": group, **event},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:32]
            actions.append(
                {
                    "provider_id": str(event.get("id") or f"alpaca-{digest}"),
                    "provider": "alpaca",
                    "action_type": action_type,
                    "symbol": symbol,
                    "process_date": str(process_date),
                    "ex_date": str(event.get("ex_date") or process_date),
                    "record_date": (
                        str(event["record_date"]) if event.get("record_date") else None
                    ),
                    "payable_date": (
                        str(event["payable_date"]) if event.get("payable_date") else None
                    ),
                    "old_rate": (
                        float(event["old_rate"]) if event.get("old_rate") is not None else None
                    ),
                    "new_rate": (
                        float(event["new_rate"]) if event.get("new_rate") is not None else None
                    ),
                    "cash_rate": (
                        float(event["rate"])
                        if action_type == "cash_dividend" and event.get("rate") is not None
                        else None
                    ),
                    "payload": {"group": group, **event},
                }
            )
    token = payload.get("next_page_token")
    if token is None and isinstance(payload.get("meta"), dict):
        token = payload["meta"].get("next_page_token")
    return actions, token


def fetch_corporate_actions(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
) -> list[dict]:
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        raise BacktestDataError(
            "缺少 Alpaca 凭据，无法核验拆股和分红；为避免错误结果，回测已停止。"
        )
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    actions: list[dict] = []
    token = None
    while True:
        params = {
            "symbols": ",".join(normalized),
            "start": start_date,
            "end": end_date,
            "limit": 1000,
            "sort": "asc",
        }
        if token:
            params["page_token"] = token
        try:
            response = requests.get(
                CORPORATE_ACTIONS_URL,
                params=params,
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
                "无法从 Alpaca 核验公司行动；为避免拆股或分红造成错误结果，回测已停止。",
                detail=str(exc),
            ) from exc
        if not isinstance(payload, dict):
            raise BacktestDataError("Alpaca 公司行动响应格式异常。")
        page_actions, token = _normalize_payload(payload)
        actions.extend(page_actions)
        if not token:
            break
    return actions


def ensure_corporate_actions(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
) -> list[dict]:
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    states = {
        symbol: backtest_repository.corporate_action_coverage(symbol)
        for symbol in normalized
    }
    covered = all(
        state
        and state["status"] == "success"
        and _coverage_is_fresh(state)
        and state["coverage_start"] <= start_date
        and state["coverage_end"] >= end_date
        for state in states.values()
    )
    if not covered:
        # The sync-state table stores one continuous interval.  When a request
        # extends either side of a previous interval, refetch the whole union
        # so MIN(start)/MAX(end) can never conceal an unverified hole.
        successful_starts = [
            state["coverage_start"]
            for state in states.values()
            if state and state["status"] == "success"
        ]
        successful_ends = [
            state["coverage_end"]
            for state in states.values()
            if state and state["status"] == "success"
        ]
        fetch_start = min([start_date, *successful_starts])
        fetch_end = max([end_date, *successful_ends])
        try:
            actions = fetch_corporate_actions(
                normalized,
                start_date=fetch_start,
                end_date=fetch_end,
            )
            backtest_repository.upsert_corporate_actions(
                actions,
                symbols=normalized,
                coverage_start=fetch_start,
                coverage_end=fetch_end,
            )
        except Exception as exc:
            backtest_repository.mark_corporate_action_sync_error(
                normalized,
                coverage_start=fetch_start,
                coverage_end=fetch_end,
                error=str(exc),
            )
            raise
    return backtest_repository.get_corporate_actions(
        normalized,
        start_date=start_date,
        end_date=end_date,
    )


def validate_supported_actions(actions: list[dict]) -> None:
    unsupported = [
        {
            "symbol": action["symbol"],
            "action_type": action["action_type"],
            "date": action.get("ex_date") or action["process_date"],
        }
        for action in actions
        if action["action_type"] not in SUPPORTED_ACTIONS
    ]
    if unsupported:
        raise BacktestDataError(
            "回测区间包含暂不支持的公司行动，不能保证账户结果正确。",
            detail=unsupported,
        )
