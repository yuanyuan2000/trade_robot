from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from database import backtest_repository, realtime_repository, repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.data import EventPrice, HistoricalDataSet
from services.backtest.dsl import compile_expression
from services.backtest.engine import CodeEventContext
from services.backtest.portfolio import Portfolio
from services.backtest.validation import validate_strategy_payload
from services.realtime_panel_script import validate_panel_script
from services.market_context import (
    filter_rows_for_market,
    market_sessions,
    normalize_market_config,
)


_CACHE_SECONDS = 30.0
_NEW_YORK = ZoneInfo("America/New_York")
_cache_lock = threading.RLock()
_cache: dict[int, tuple[float, str, dict]] = {}


def _finite(value: Any) -> float | bool | str | None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _effective_strategy(task: dict) -> tuple[dict, str]:
    if task.get("runtime_state") in {"starting", "running", "degraded", "stopping"}:
        runs = realtime_repository.list_runs(task["id"], limit=1)
        if runs:
            return deepcopy(runs[0]["strategy_snapshot"]), "run_snapshot"
    return deepcopy(task["strategy_snapshot"]), "task_snapshot"


def _cache_signature(
    task: dict,
    overview: dict,
    strategy: dict,
    candidate_snapshot_source: str,
) -> str:
    prices = ";".join(
        f"{item['symbol']}:{item.get('latest_date')}:{item.get('latest_price_updated_at')}:{item.get('latest_price')}"
        for item in overview.get("items", [])
    )
    return (
        f"{task.get('panel_revision')}|{task.get('revision')}|"
        f"{task.get('runtime_state')}|{candidate_snapshot_source}|"
        f"{strategy.get('revision')}|{prices}"
    )


def _dataset_for_overview(
    overview: dict,
    strategy: dict,
) -> tuple[HistoricalDataSet, dict, str]:
    current_day = datetime.now(timezone.utc).astimezone(_NEW_YORK).date()
    market = normalize_market_config(strategy.get("market"))
    sessions = market_sessions(
        (current_day - timedelta(days=14)).isoformat(),
        current_day.isoformat(),
        market,
    )
    current_date = str(sessions[-1]["trading_date"])
    is_current_session = current_date == current_day.isoformat()
    daily: dict[str, list[dict]] = {}
    event_prices: dict[str, EventPrice] = {}
    cumulative: dict[str, dict[str, float]] = {}
    symbols: list[str] = []
    for item in overview.get("items", []):
        symbol = str(item["symbol"]).upper()
        rows = repository.get_strategy_daily_prices(
            symbol,
            market["type"],
            include_metadata=True,
        )
        rows = filter_rows_for_market(rows, market)
        if not rows:
            continue
        latest_row = dict(rows[-1])
        latest_price = (
            item.get("latest_price")
            if is_current_session
            else latest_row.get("close")
        )
        if latest_price is None:
            continue
        history = [dict(row) for row in rows[:-1]]
        synthetic = {
            **latest_row,
            "date": current_date,
            "close": float(latest_price),
            "open": float(latest_row.get("open") or latest_price),
            "high": max(float(latest_row.get("high") or latest_price), float(latest_price)),
            "low": min(float(latest_row.get("low") or latest_price), float(latest_price)),
        }
        # A stale database row is still the latest known observation. Moving
        # only that observation to the common as-of date avoids counting it
        # twice and keeps cross-symbol ranking point-in-time consistent.
        daily[symbol] = [*history, synthetic]
        event_prices[symbol] = EventPrice(
            signal_price=float(latest_price),
            fill_price=None,
            signal_time=(
                item.get("latest_price_updated_at") or item.get("latest_date") or current_date
                if is_current_session
                else latest_row.get("date") or current_date
            ),
            fill_time=None,
        )
        volume = float(latest_row.get("volume") or 0)
        cumulative[symbol] = {
            f"{current_date}|LATEST": volume,
        }
        symbols.append(symbol)
    actions = []
    if symbols:
        actions = backtest_repository.get_corporate_actions(
            symbols,
            start_date=(
                datetime.now(timezone.utc).astimezone(_NEW_YORK).date()
                - timedelta(days=550)
            ).isoformat(),
            end_date=current_date,
        )
    dataset = HistoricalDataSet(
        daily=daily,
        sessions=[current_date],
        cumulative_volumes=cumulative,
        availability_start={symbol: rows[0]["date"] for symbol, rows in daily.items()},
        corporate_actions=actions,
        manifest={"source": "database", "as_of": current_date, "market": market},
    )
    return dataset, event_prices, current_date


def _context(
    dataset: HistoricalDataSet,
    event_prices: dict,
    trading_date: str,
    symbol: str,
    event: str,
    logs: list[dict],
) -> CodeEventContext:
    price = event_prices[symbol].signal_price
    portfolio = Portfolio(100_000)
    latest_volume = dataset.cumulative_volumes.get(symbol, {}).get(
        f"{trading_date}|LATEST"
    )
    if latest_volume is not None:
        dataset.cumulative_volumes.setdefault(symbol, {}).setdefault(
            f"{trading_date}|{event}", latest_volume
        )

    def capture(level, event_type, message, **kwargs):
        logs.append({
            "level": level,
            "event_type": event_type,
            "message": message,
            "symbol": kwargs.get("symbol"),
            "context": kwargs.get("context") or {},
        })

    return CodeEventContext(
        dataset=dataset,
        portfolio=portfolio,
        universe=[symbol],
        trading_date=trading_date,
        event=event,
        event_prices={symbol: event_prices[symbol]},
        marks={symbol: price},
        all_candidate_symbols=[symbol],
        log_callback=capture,
    )


def _code_row(
    strategy: dict,
    dataset: HistoricalDataSet,
    event_prices: dict,
    trading_date: str,
    symbol: str,
) -> dict:
    code_key = strategy["code_key"]
    strategy_type = get_code_strategy(code_key)
    instance = strategy_type(strategy["definition"].get("params", {}))
    logs: list[dict] = []

    def context_factory(event: str) -> CodeEventContext:
        return _context(
            dataset, event_prices, trading_date, symbol, event, logs
        )

    result = instance.observe_latest(context_factory, logs)
    if result is None:
        raise ValueError(f"代码策略 {code_key} 未提供最新行情观察。")
    return {
        **result,
        "eligible": bool(result.get("eligible")),
        "reasons": list(result.get("reasons") or []),
        "score": _finite(result.get("score")),
        "metrics": {
            key: _finite(value)
            for key, value in dict(result.get("metrics") or {}).items()
        },
        "details": dict(result.get("details") or {}),
    }


def _visual_columns(task: dict) -> tuple[list[dict], dict]:
    parsed = validate_panel_script(str((task.get("panel_settings") or {}).get("script") or ""))
    return parsed["columns"], parsed["default_sort"]


def _visual_row(
    strategy: dict,
    columns: list[dict],
    dataset: HistoricalDataSet,
    event_prices: dict,
    trading_date: str,
    symbol: str,
) -> dict:
    definition = strategy["definition"]
    default_event = (
        (definition.get("competition") or {}).get("when")
        or next((rule.get("when") for rule in definition.get("rules", []) if rule.get("enabled")), "OPEN")
    )
    metrics: dict[str, Any] = {}
    details: dict[str, Any] = {"columns": {}, "rules": []}
    for column in columns:
        context = dataset.expression_context(
            symbol=symbol,
            trading_date=trading_date,
            event=str(column.get("event") or default_event),
            price=event_prices[symbol].signal_price,
            position=0,
        )
        compiled = compile_expression(column["expression"])
        value = compiled.evaluate(context)
        metrics[column["key"]] = _finite(value)
        details["columns"][column["key"]] = {
            "expression": column["expression"],
            "inputs": compiled.resolve_inputs(context),
        }
    matched_risk: list[str] = []
    matched_rules: list[str] = []
    for rule in definition.get("rules", []):
        if not rule.get("enabled", True):
            continue
        expression = compile_expression(rule.get("condition") or "true")
        context = dataset.expression_context(
            symbol=symbol,
            trading_date=trading_date,
            event=rule.get("when") or "OPEN",
            price=event_prices[symbol].signal_price,
            position=0,
        )
        matched = bool(expression.evaluate(context))
        detail = {
            "name": rule.get("name"), "action": rule.get("action"),
            "event": rule.get("when"), "matched": matched,
            "condition": rule.get("condition"),
        }
        details["rules"].append(detail)
        if matched:
            matched_rules.append(
                f"{rule.get('action', 'HOLD')}：{rule.get('name') or rule.get('id') or '规则'}"
            )
        if matched and rule.get("action") in {"SELL", "HOLD"}:
            matched_risk.append(str(rule.get("name") or rule.get("id") or "风险规则"))
    eligible = True
    score = None
    if strategy.get("selection_mode") == "competition":
        competition = definition["competition"]
        eligibility_context = dataset.expression_context(
            symbol=symbol, trading_date=trading_date,
            event=competition.get("eligibility_when", competition["when"]),
            price=event_prices[symbol].signal_price, position=0,
        )
        score_context = dataset.expression_context(
            symbol=symbol, trading_date=trading_date,
            event=competition["when"],
            price=event_prices[symbol].signal_price, position=0,
        )
        eligible = bool(
            compile_expression(competition["eligibility"]).evaluate(
                eligibility_context
            )
        )
        score = _finite(
            compile_expression(competition["score"]).evaluate(score_context)
        )
        minimum_score = competition.get("minimum_score")
        passes_minimum = (
            score is not None
            and (minimum_score is None or score >= float(minimum_score))
        )
        details["competition"] = {
            "eligible": eligible,
            "score": score,
            "minimum_score": minimum_score,
            "passes_minimum_score": passes_minimum,
        }
        eligible = eligible and passes_minimum
    if strategy.get("selection_mode") == "competition":
        reasons = list(matched_risk)
        if not eligible:
            competition_detail = details["competition"]
            reasons.append(
                "评分低于最低可入选评分"
                if competition_detail["eligible"]
                and not competition_detail["passes_minimum_score"]
                else "不满足候选条件"
            )
        effective_eligible = eligible and not matched_risk
        status = "通过" if effective_eligible else "已过滤"
    else:
        reasons = matched_rules
        effective_eligible = True
        status = "有信号" if matched_rules else "观察"
    return {
        "eligible": effective_eligible,
        "reasons": reasons,
        "score": score,
        "metrics": metrics,
        "details": details,
        "status_override": status,
    }


def build_realtime_dashboard(task_id: int, *, force: bool = False) -> dict:
    """Read internal market-overview data and calculate one strategy panel."""
    task = realtime_repository.get_task(task_id)
    strategy_raw, candidate_snapshot_source = _effective_strategy(task)
    strategy = validate_strategy_payload(strategy_raw)
    overview = repository.list_market_overview()
    signature = _cache_signature(
        task, overview, strategy_raw, candidate_snapshot_source
    )
    with _cache_lock:
        cached = _cache.get(int(task_id))
        if not force and cached and cached[1] == signature and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return deepcopy(cached[2])

    dataset, event_prices, trading_date = _dataset_for_overview(overview, strategy)
    candidates = {
        str(item["symbol"]).upper()
        for item in strategy["definition"].get("symbols", [])
    }
    code_observer = None
    if strategy["design_mode"] == "code":
        params = strategy["definition"].get("params", {})
        code_observer = get_code_strategy(strategy["code_key"])(params)
        columns = [dict(column) for column in code_observer.observation_columns()]
        default_sort = {"key": "score", "direction": "desc"}
    else:
        columns, default_sort = _visual_columns(task)

    overview_by_symbol = {
        str(item["symbol"]).upper(): item for item in overview.get("items", [])
    }
    rows: list[dict] = []
    for symbol, item in overview_by_symbol.items():
        row = {
            "symbol": symbol,
            "display_symbol": item.get("display_symbol") or symbol,
            "name": item.get("name"),
            "latest_price": _finite(item.get("latest_price")),
            "price_updated_at": item.get("latest_price_updated_at"),
            "price_is_provisional": item.get("latest_price_is_provisional"),
            "price_source": item.get("latest_price_source"),
            "price_timeframe": item.get("latest_price_timeframe"),
            "price_basis": item.get("latest_price_basis"),
            "data_date": item.get("latest_date"),
            "is_candidate": symbol in candidates,
            "eligible": False,
            "status": "不可计算",
            "reason": "缺少内部行情数据",
            "metrics": {},
            "details": {},
            "rank": None,
            "selected_for_target": False,
        }
        if symbol not in event_prices:
            rows.append(row)
            continue
        try:
            result = (
                _code_row(strategy, dataset, event_prices, trading_date, symbol)
                if strategy["design_mode"] == "code"
                else _visual_row(strategy, columns, dataset, event_prices, trading_date, symbol)
            )
            row.update(result)
            row["status"] = result.get(
                "status_override", "通过" if result["eligible"] else "已过滤"
            )
            row["reason"] = "、".join(result["reasons"]) if result["reasons"] else "—"
        except Exception as exc:
            row["reason"] = str(exc)
            row["details"] = {"error": str(exc)}
        rows.append(row)

    ranked = sorted(
        (
            row for row in rows
            if row["is_candidate"] and row["eligible"] and row.get("score") is not None
        ),
        key=lambda row: (-float(row["score"]), row["symbol"]),
    )
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    target_count = 0
    if strategy["selection_mode"] == "competition":
        if code_observer is not None:
            target_count = max(0, int(code_observer.observation_target_count()))
        else:
            target_count = 1
    for row in ranked[:target_count]:
        row["selected_for_target"] = True

    payload = {
        "task_id": int(task_id),
        "source": "market_overview_database",
        "external_api_called": False,
        "observation_mode": "strategy_latest_simulation",
        "formal_decision": False,
        "observation_note": (
            "按任务策略定义使用内部数据库最新行情模拟当前时点；"
            "不是正式决策，可能与行情页标准指标不同。"
        ),
        "calculated_at": repository.utc_now_iso(),
        "strategy_name": strategy.get("name"),
        "design_mode": strategy["design_mode"],
        "selection_mode": strategy["selection_mode"],
        "candidate_snapshot_source": candidate_snapshot_source,
        "columns": columns,
        "default_sort": default_sort,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "candidates": sum(1 for row in rows if row["is_candidate"]),
            "eligible": sum(1 for row in rows if row["eligible"]),
            "filtered": sum(1 for row in rows if row["status"] == "已过滤"),
            "unavailable": sum(1 for row in rows if row["status"] == "不可计算"),
        },
    }
    with _cache_lock:
        _cache[int(task_id)] = (time.monotonic(), signature, deepcopy(payload))
    return payload


def clear_realtime_dashboard_cache(task_id: int) -> None:
    with _cache_lock:
        _cache.pop(int(task_id), None)


def dashboard_recommendations(dashboard: dict, *, limit: int = 3) -> list[dict]:
    """Return the highest-ranked eligible overview symbols for a competition card."""
    if dashboard.get("selection_mode") != "competition":
        return []
    ranked = sorted(
        (
            row for row in dashboard.get("rows", [])
            if row.get("eligible") and row.get("rank") is not None
        ),
        key=lambda row: int(row["rank"]),
    )
    return [
        {
            "symbol": row["symbol"],
            "display_symbol": row.get("display_symbol") or row["symbol"],
            "score": row.get("score"),
            "rank": int(row["rank"]),
        }
        for row in ranked[:max(0, int(limit))]
    ]
