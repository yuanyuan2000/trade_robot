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
SUPPORTED_ACTIONS = {
    "forward_split",
    "reverse_split",
    "cash_dividend",
    "name_change",
}
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


def _leg(
    role: str,
    event: dict,
    *,
    symbol_key: str,
    cusip_key: str | None = None,
    isin_key: str | None = None,
    share_rate_key: str | None = None,
    cash_rate_key: str | None = None,
) -> dict | None:
    symbol = event.get(symbol_key)
    cusip = event.get(cusip_key) if cusip_key else None
    isin = event.get(isin_key) if isin_key else None
    if not symbol and not cusip and not isin:
        return None
    return {
        "role": role,
        "symbol": str(symbol).upper() if symbol else None,
        "cusip": str(cusip) if cusip else None,
        "isin": str(isin) if isin else None,
        "share_rate": (
            float(event[share_rate_key])
            if share_rate_key and event.get(share_rate_key) is not None
            else None
        ),
        "cash_rate": (
            float(event[cash_rate_key])
            if cash_rate_key and event.get(cash_rate_key) is not None
            else None
        ),
        "currency": event.get("currency"),
    }


def _event_legs(group: str, event: dict) -> list[dict]:
    candidates: list[dict | None]
    if group == "name_changes":
        candidates = [
            _leg("source", event, symbol_key="old_symbol", cusip_key="old_cusip", isin_key="old_isin"),
            _leg("target", event, symbol_key="new_symbol", cusip_key="new_cusip", isin_key="new_isin"),
        ]
    elif group == "spin_offs":
        candidates = [
            _leg("source", event, symbol_key="source_symbol", cusip_key="source_cusip", isin_key="source_isin", share_rate_key="source_rate"),
            _leg("target", event, symbol_key="new_symbol", cusip_key="new_cusip", isin_key="new_isin", share_rate_key="new_rate"),
        ]
    elif group in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}:
        candidates = [
            _leg("acquiree", event, symbol_key="acquiree_symbol", cusip_key="acquiree_cusip", isin_key="acquiree_isin", share_rate_key="acquiree_rate"),
            _leg("acquirer", event, symbol_key="acquirer_symbol", cusip_key="acquirer_cusip", isin_key="acquirer_isin", share_rate_key="acquirer_rate", cash_rate_key="rate"),
        ]
    else:
        candidates = [
            _leg(
                "subject",
                event,
                symbol_key="symbol",
                cusip_key="cusip",
                isin_key="isin",
                share_rate_key="new_rate",
                cash_rate_key="rate",
            )
        ]
    return [item for item in candidates if item is not None]


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
                    "currency": event.get("currency"),
                    "region": event.get("region") or "us",
                    "sub_type": event.get("sub_type"),
                    "special": bool(event.get("special")),
                    "foreign": bool(event.get("foreign")),
                    "due_bill_on_date": event.get("due_bill_on_date"),
                    "due_bill_off_date": event.get("due_bill_off_date"),
                    "effective_date": str(
                        event.get("effective_date")
                        or event.get("ex_date")
                        or process_date
                    ),
                    "legs": _event_legs(group, event),
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


def _role_affects_position(action_type: str, role: str) -> bool:
    if action_type == "name_change":
        return False
    if action_type == "spin_off":
        return role == "source"
    if action_type in {"cash_merger", "stock_merger", "stock_and_cash_merger"}:
        return role == "acquiree"
    return role in {"subject", "source", "acquiree"}


def _actions_for_symbols(
    actions: list[dict],
    symbols: list[str],
    required_starts: dict[str, str],
) -> list[dict]:
    requested = set(symbols)
    matched: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for action in actions:
        effective_date = str(
            action.get("effective_date")
            or action.get("ex_date")
            or action["process_date"]
        )
        legs = action.get("legs") or [
            {"role": "subject", "symbol": action.get("symbol")}
        ]
        for leg in legs:
            symbol = str(leg.get("symbol") or "").upper()
            if symbol not in requested or effective_date < required_starts[symbol]:
                continue
            role = str(leg.get("role") or "subject")
            key = (str(action["provider_id"]), symbol, role)
            if key in seen:
                continue
            seen.add(key)
            item = dict(action)
            item["event_symbol"] = action.get("symbol")
            item["symbol"] = symbol
            item["matched_role"] = role
            item["matched_leg"] = dict(leg)
            item["affects_position"] = _role_affects_position(
                item["action_type"], role
            )
            matched.append(item)
    return matched


def ensure_corporate_actions(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
    symbol_starts: dict[str, str] | None = None,
) -> list[dict]:
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    required_starts = {
        symbol: max(start_date, (symbol_starts or {}).get(symbol, start_date))
        for symbol in normalized
    }
    states = {
        symbol: backtest_repository.corporate_action_coverage(symbol)
        for symbol in normalized
    }
    covered = all(
        state
        and state["status"] == "success"
        and _coverage_is_fresh(state)
        and state["coverage_start"] <= required_starts[symbol]
        and state["coverage_end"] >= end_date
        for symbol, state in states.items()
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
        fetch_start = min([min(required_starts.values()), *successful_starts])
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
    stored = backtest_repository.get_corporate_actions(
        normalized,
        start_date=min(required_starts.values()),
        end_date=end_date,
    )
    return _actions_for_symbols(stored, normalized, required_starts)


def validate_supported_actions(actions: list[dict]) -> None:
    unsupported = [
        {
            "symbol": action["symbol"],
            "action_type": action["action_type"],
            "date": action.get("ex_date") or action["process_date"],
        }
        for action in actions
        if action["action_type"] not in SUPPORTED_ACTIONS
        and action.get("affects_position", True)
    ]
    if unsupported:
        raise BacktestDataError(
            "回测区间包含暂不支持的公司行动，不能保证账户结果正确。",
            detail=unsupported,
        )
    incomplete = []
    for action in actions:
        if not action.get("affects_position", True):
            continue
        action_type = action["action_type"]
        missing: list[str] = []
        if action_type in {"forward_split", "reverse_split"}:
            if action.get("old_rate") is None:
                missing.append("old_rate")
            if action.get("new_rate") is None:
                missing.append("new_rate")
        elif action_type == "cash_dividend":
            if action.get("cash_rate") is None:
                missing.append("cash_rate")
            if not action.get("payable_date"):
                missing.append("payable_date")
        if missing:
            incomplete.append(
                {
                    "provider_id": action.get("provider_id"),
                    "symbol": action["symbol"],
                    "action_type": action_type,
                    "date": action.get("ex_date") or action["process_date"],
                    "missing_fields": missing,
                }
            )
    if incomplete:
        raise BacktestDataError(
            "回测区间的公司行动缺少关键字段，不能安全推断现金或持仓变化。",
            detail=incomplete,
        )
