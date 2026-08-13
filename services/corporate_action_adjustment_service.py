from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from services.backtest.corporate_actions import (
    ensure_corporate_actions,
    get_stored_corporate_actions,
)


ADJUSTMENT_MODES = {"raw", "split", "all"}


def _row_date(row: dict) -> str:
    return str(row["date"])[:10]


def _public_action(action: dict) -> dict:
    return {
        "provider_id": action.get("provider_id"),
        "action_type": action.get("action_type"),
        "symbol": action.get("symbol"),
        "event_symbol": action.get("event_symbol"),
        "matched_role": action.get("matched_role"),
        "affects_position": bool(action.get("affects_position", True)),
        "effective_date": action.get("effective_date")
        or action.get("ex_date")
        or action.get("process_date"),
        "ex_date": action.get("ex_date"),
        "record_date": action.get("record_date"),
        "payable_date": action.get("payable_date"),
        "old_rate": action.get("old_rate"),
        "new_rate": action.get("new_rate"),
        "cash_rate": action.get("cash_rate"),
        "currency": action.get("currency"),
        "special": bool(action.get("special")),
    }


def adjust_price_rows(
    rows: Iterable[dict],
    actions: Iterable[dict],
    *,
    mode: str = "all",
) -> list[dict]:
    normalized_mode = str(mode or "all").lower()
    if normalized_mode not in ADJUSTMENT_MODES:
        raise ValueError("复权模式必须为 raw、split 或 all。")
    values = [dict(row) for row in rows]
    if normalized_mode == "raw" or not values:
        return values

    close_before: dict[str, float] = {}
    for action in actions:
        effective = str(
            action.get("effective_date")
            or action.get("ex_date")
            or action.get("process_date")
        )
        previous = [row for row in values if _row_date(row) < effective]
        if previous:
            close_before[effective] = float(previous[-1]["close"])

    dividend_by_date: dict[str, float] = defaultdict(float)
    split_events: list[tuple[str, float]] = []
    for action in actions:
        if not action.get("affects_position", True):
            continue
        effective = str(
            action.get("effective_date")
            or action.get("ex_date")
            or action.get("process_date")
        )
        if action.get("action_type") in {"forward_split", "reverse_split"}:
            old_rate = float(action.get("old_rate") or 0)
            new_rate = float(action.get("new_rate") or 0)
            if old_rate <= 0 or new_rate <= 0:
                raise ValueError(f"{action.get('symbol')} 拆股比例无效。")
            split_events.append((effective, new_rate / old_rate))
        elif normalized_mode == "all" and action.get("action_type") == "cash_dividend":
            dividend_by_date[effective] += float(action.get("cash_rate") or 0)

    result: list[dict] = []
    for row in values:
        adjusted = dict(row)
        row_date = _row_date(row)
        price_factor = 1.0
        volume_factor = 1.0
        for effective, ratio in split_events:
            if row_date < effective:
                price_factor /= ratio
                volume_factor *= ratio
        for effective, cash_rate in dividend_by_date.items():
            if row_date >= effective:
                continue
            previous_close = close_before.get(effective)
            if previous_close is None or previous_close <= 0 or cash_rate >= previous_close:
                raise ValueError(
                    f"{effective} 现金分红缺少有效除息日前收盘价，无法安全复权。"
                )
            price_factor *= (previous_close - cash_rate) / previous_close
        for field in ("open", "high", "low", "close"):
            adjusted[field] = float(adjusted[field]) * price_factor
        adjusted["volume"] = float(adjusted.get("volume") or 0) * volume_factor
        adjusted["adjustment_factor"] = price_factor
        result.append(adjusted)
    return result


def adjusted_daily_payload(
    symbol: str,
    rows: list[dict],
    symbol_settings: dict,
    *,
    mode: str = "all",
) -> dict:
    normalized_mode = str(mode or "all").lower()
    if normalized_mode not in ADJUSTMENT_MODES:
        raise ValueError("复权模式必须为 raw、split 或 all。")
    if not rows or normalized_mode == "raw" or symbol_settings.get("asset_class") != "us_equity":
        return {
            "rows": [dict(row) for row in rows],
            "actions": [],
            "adjustment": "raw",
            "warning": None,
        }

    bases = {
        str(row.get("price_basis") or "unknown").lower()
        for row in rows
    }
    if bases != {"raw"}:
        return {
            "rows": [dict(row) for row in rows],
            "actions": [],
            "adjustment": "raw",
            "warning": (
                f"{symbol} 行情价格口径为 {', '.join(sorted(bases))}，"
                "为避免二次复权，当前显示原始入库数据。"
            ),
        }

    first_date = _row_date(rows[0])
    # Use the actual first stored bar. Some legacy history_start_date values
    # describe a recent import rather than the instrument's true history.
    start_date = first_date
    actions = ensure_corporate_actions(
        [symbol],
        start_date=start_date,
        end_date=_row_date(rows[-1]),
        symbol_starts={symbol: start_date},
    )
    adjusted = adjust_price_rows(rows, actions, mode=normalized_mode)
    return {
        "rows": adjusted,
        "actions": [_public_action(action) for action in actions],
        "adjustment": normalized_mode,
        "warning": None,
    }


def stored_adjusted_daily_payload(
    symbol: str,
    rows: list[dict],
    symbol_settings: dict,
    *,
    mode: str = "all",
) -> dict:
    """Adjust display rows using only company actions already in SQLite.

    The market overview is a read-only database view and must not refresh an
    external corporate-action cache as a side effect.  Missing cached events
    simply mean there are no locally known adjustments; the response records
    that it used the stored-only contract for auditability.
    """
    normalized_mode = str(mode or "all").lower()
    if normalized_mode not in ADJUSTMENT_MODES:
        raise ValueError("复权模式必须为 raw、split 或 all。")
    if not rows or normalized_mode == "raw" or symbol_settings.get("asset_class") != "us_equity":
        return {
            "rows": [dict(row) for row in rows],
            "actions": [],
            "adjustment": "raw",
            "warning": None,
            "action_source": "stored_only",
        }

    bases = {
        str(row.get("price_basis") or "unknown").lower()
        for row in rows
    }
    if bases != {"raw"}:
        return {
            "rows": [dict(row) for row in rows],
            "actions": [],
            "adjustment": "raw",
            "warning": (
                f"{symbol} 行情价格口径为 {', '.join(sorted(bases))}，"
                "为避免二次复权，行情总览指标使用原始入库数据。"
            ),
            "action_source": "stored_only",
        }

    start_date = _row_date(rows[0])
    actions = get_stored_corporate_actions(
        [symbol],
        start_date=start_date,
        end_date=_row_date(rows[-1]),
        symbol_starts={symbol: start_date},
    )
    adjusted = adjust_price_rows(rows, actions, mode=normalized_mode)
    return {
        "rows": adjusted,
        "actions": [_public_action(action) for action in actions],
        "adjustment": normalized_mode,
        "warning": None,
        "action_source": "stored_only",
    }
