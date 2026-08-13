from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
import threading
import time
from typing import Any

from database import backtest_repository, realtime_repository, repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.data import EventPrice, HistoricalDataSet
from services.backtest.dsl import compile_expression
from services.backtest.engine import CodeEventContext
from services.backtest.portfolio import Portfolio
from services.backtest.validation import validate_strategy_payload
from services.realtime_panel_script import validate_panel_script


_CACHE_SECONDS = 30.0
_cache_lock = threading.RLock()
_cache: dict[int, tuple[float, str, dict]] = {}


def _code_columns(code_key: str, params: dict) -> list[dict]:
    """Build concise column metadata with the task's effective parameters."""
    if code_key == "sevenstar_etf_rotation":
        lookback = int(params["lookback_days"])
        short = int(params["short_lookback_days"])
        volume_days = int(params["volume_lookback_days"])
        formula_mode = (
            "一致加权 R²（回归、均值和离差均使用 w² 权重）"
            if params["trend_formula_mode"] == "consistent_w2"
            else "历史 v1.0.0 兼容口径（回归与 R² 使用不同权重）"
        )
        return [
            {
                "key": "annualized_returns", "label": "长期年化趋势", "format": "percent",
                "help": (
                    f"取此前 {lookback} 个完整交易日收盘价并加入当前价格，共 {lookback + 1} 个点；"
                    "对数价格按越近权重越高的线性回归求斜率，再用 expm1(斜率×250) 折算年化趋势。"
                ),
            },
            {
                "key": "r_squared", "label": "R²", "format": "number",
                "help": (
                    f"使用与长期趋势相同的 {lookback + 1} 个价格点计算拟合优度；当前采用{formula_mode}。"
                    "越接近 1 表示价格走势越能被这条趋势线解释。"
                ),
            },
            {
                "key": "short_annualized", "label": "短动量", "format": "percent",
                "help": (
                    f"当前价格相对此前第 {short} 个完整交易日的收盘价计算简单收益，"
                    f"再按 250/{short} 次方折算年化；低于 {float(params['short_momentum_threshold_percent']):g}%"
                    "时会命中短期动量过滤（若该过滤已启用）。"
                ),
            },
            {
                "key": "volume_ratio", "label": "量比", "format": "number",
                "help": (
                    f"当前数据库最新成交量 ÷ 此前 {volume_days} 个完整交易日平均成交量。"
                    f"量比大于 {float(params['volume_ratio_threshold']):g}，且长期年化趋势大于 "
                    f"{float(params['volume_return_limit_percent']):g}% 时命中放量过热过滤。"
                ),
                "conditional": "enable_volume_check",
            },
            {
                "key": "score", "label": "最终评分", "format": "number",
                "help": (
                    "最终评分 = 长期年化趋势 × R²。只有通过全部过滤且评分严格位于 "
                    f"({float(params['min_score_threshold']):g}, {float(params['max_score_threshold']):g}) "
                    f"之间的标的才参与排名，面板取前 {int(params['holdings_num'])} 只作为观察目标。"
                ),
            },
        ]
    if code_key == "rapid_drop_atr_rotation":
        momentum = int(params["momentum_lookback_sessions"])
        atr_period = int(params["atr_period"])
        weighting = {
            "wilder": "Wilder 平滑",
            "ema": "EMA 加权",
            "linear": "线性加权",
            "simple": "简单平均",
        }.get(params["atr_weighting"], str(params["atr_weighting"]))
        filters = []
        if params["enable_percent_drop_filter"]:
            filters.append(f"单日跌幅达到 {float(params['drop_threshold_percent']):g}%")
        if params["enable_atr_drop_filter"]:
            filters.append(f"单日下跌达到 {float(params['drop_threshold_atr']):g} 倍 ATR")
        filter_text = "、".join(filters) or "未启用急跌过滤"
        return [
            {
                "key": "price_displacement", "label": "N 日价格位移", "format": "price",
                "help": (
                    f"当前价格减去此前第 {momentum} 个完整交易日的收盘价，单位与价格相同；"
                    "正值表示观察窗口内上涨，负值表示下跌。"
                ),
            },
            {
                "key": "atr", "label": "ATR", "format": "price",
                "help": (
                    f"基于截至上一完整交易日的真实波幅 TR，按 {atr_period} 日周期和{weighting}计算；"
                    "不使用当日尚未完成的最高价或最低价。"
                ),
            },
            {
                "key": "score", "label": "ATR 评分", "format": "number",
                "help": (
                    f"ATR 评分 = {momentum} 日价格位移 ÷ {atr_period} 日 ATR。"
                    f"策略另检查最近 {int(params['drop_lookback_sessions'])} 个交易日：{filter_text}；"
                    f"过滤后按评分排序并取前 {int(params['holdings_num'])} 只。"
                ),
            },
        ]
    if code_key == "rapid_drop_wtme_rotation":
        period = int(params["wtme_period"])
        half_life = float(params["wtme_half_life"])
        epsilon = float(params["wtme_epsilon"])
        filter_text = (
            f"最近 {int(params['drop_lookback_sessions'])} 个交易日内，单日跌幅达到 "
            f"{float(params['drop_threshold_percent']):g}% 时过滤"
            if params["enable_percent_drop_filter"]
            else "当前未启用百分比急跌过滤"
        )
        return [
            {
                "key": "weighted_return", "label": "加权收益 Rw", "format": "number",
                "help": (
                    f"取最近 {period} 个收益观测（包含当前价格形成的最新观测），"
                    f"按指数衰减权重计算收益率均值；半衰期为 {half_life:g} 个交易日，越近权重越高。"
                ),
            },
            {
                "key": "weighted_true_range", "label": "加权波幅 Aw", "format": "number",
                "help": (
                    f"对与 Rw 相同的 {period} 个观测计算标准化真实波幅，再使用半衰期 "
                    f"{half_life:g} 的相同权重求均值；用于衡量取得这些收益所经历的价格波动。"
                ),
            },
            {
                "key": "score", "label": "WTME 评分", "format": "number",
                "help": (
                    f"WTME = 100 × Rw ÷ (Aw + {epsilon:g})。方向收益越高且路径波动越小，评分越高；"
                    f"{filter_text}，过滤后选择评分最高的标的。"
                ),
            },
        ]
    return []


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


def _dataset_for_overview(overview: dict) -> tuple[HistoricalDataSet, dict, str]:
    current_date = date.today().isoformat()
    daily: dict[str, list[dict]] = {}
    event_prices: dict[str, EventPrice] = {}
    cumulative: dict[str, dict[str, float]] = {}
    symbols: list[str] = []
    for item in overview.get("items", []):
        symbol = str(item["symbol"]).upper()
        latest_price = item.get("latest_price")
        rows = repository.get_daily_prices(symbol, include_metadata=True)
        if latest_price is None or not rows:
            continue
        latest_row = dict(rows[-1])
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
            signal_time=item.get("latest_price_updated_at") or item.get("latest_date") or current_date,
            fill_time=None,
        )
        volume = float(latest_row.get("volume") or 0)
        cumulative[symbol] = {
            f"{current_date}|14:00": volume,
        }
        symbols.append(symbol)
    actions = []
    if symbols:
        actions = backtest_repository.get_corporate_actions(
            symbols,
            start_date=(date.today() - timedelta(days=550)).isoformat(),
            end_date=current_date,
        )
    dataset = HistoricalDataSet(
        daily=daily,
        sessions=[current_date],
        cumulative_volumes=cumulative,
        availability_start={symbol: rows[0]["date"] for symbol, rows in daily.items()},
        corporate_actions=actions,
        manifest={"source": "database", "as_of": current_date},
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
    if code_key == "sevenstar_etf_rotation":
        event = instance.params["sell_time"]
        dataset.cumulative_volumes.setdefault(symbol, {})[
            f"{trading_date}|{event}"
        ] = dataset.cumulative_volumes.get(symbol, {}).get(
            f"{trading_date}|14:00", 0.0
        )
        context = _context(dataset, event_prices, trading_date, symbol, event, logs)
        metrics = instance._metrics(context, symbol)
        details = dict(metrics)
        return {
            "eligible": bool(metrics["eligible"]),
            "reasons": list(metrics.get("filter_reasons") or []),
            "score": _finite(metrics.get("score")),
            "metrics": {
                key: _finite(metrics.get(key))
                for key in (
                    "annualized_returns", "r_squared", "short_annualized",
                    "volume_ratio", "score",
                )
            },
            "details": details,
        }
    if code_key == "rapid_drop_atr_rotation":
        risk = _context(
            dataset, event_prices, trading_date, symbol,
            instance.params["risk_check_time"], logs,
        )
        instance._risk_check(risk)
        selection = _context(
            dataset, event_prices, trading_date, symbol,
            instance.params["selection_time"], logs,
        )
        instance._select(selection)
        evaluation = next(
            item["context"] for item in reversed(logs)
            if item["event_type"] == "RAPID_DROP_ATR_DAILY_SCORE"
        )
        return {
            "eligible": not bool(evaluation.get("filter_codes")),
            "reasons": list(evaluation.get("filter_reasons") or []),
            "score": _finite(evaluation.get("score")),
            "metrics": {
                "price_displacement": _finite(
                    float(evaluation["current_price"]) - float(evaluation["base_price"])
                ),
                "atr": _finite(evaluation.get("atr")),
                "score": _finite(evaluation.get("score")),
            },
            "details": evaluation,
        }
    if code_key == "rapid_drop_wtme_rotation":
        risk = _context(
            dataset, event_prices, trading_date, symbol,
            instance.params["risk_check_time"], logs,
        )
        instance._risk_check(risk)
        selection = _context(
            dataset, event_prices, trading_date, symbol,
            instance.params["selection_time"], logs,
        )
        instance._select(selection)
        evaluation = next(
            item["context"] for item in reversed(logs)
            if item["event_type"] == "RAPID_DROP_WTME_DAILY_SCORE"
        )
        return {
            "eligible": not bool(evaluation.get("filter_codes")),
            "reasons": list(evaluation.get("filter_reasons") or []),
            "score": _finite(evaluation.get("score")),
            "metrics": {
                "weighted_return": _finite(evaluation.get("weighted_return")),
                "weighted_true_range": _finite(evaluation.get("weighted_true_range")),
                "score": _finite(evaluation.get("score")),
            },
            "details": evaluation,
        }
    raise ValueError(f"代码策略 {code_key} 尚未定义实时面板。")


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

    dataset, event_prices, trading_date = _dataset_for_overview(overview)
    candidates = {
        str(item["symbol"]).upper()
        for item in strategy["definition"].get("symbols", [])
    }
    if strategy["design_mode"] == "code":
        params = strategy["definition"].get("params", {})
        columns = [
            dict(column)
            for column in _code_columns(strategy["code_key"], params)
            if not column.get("conditional") or params.get(column["conditional"])
        ]
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
            if row["eligible"] and row.get("score") is not None
        ),
        key=lambda row: (-float(row["score"]), row["symbol"]),
    )
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    target_count = 0
    if strategy["selection_mode"] == "competition":
        if strategy["design_mode"] == "code" and strategy["code_key"] in {
            "sevenstar_etf_rotation", "rapid_drop_atr_rotation",
        }:
            target_count = int(strategy["definition"].get("params", {}).get("holdings_num", 1))
        else:
            target_count = 1
    for row in ranked[:target_count]:
        row["selected_for_target"] = True

    payload = {
        "task_id": int(task_id),
        "source": "market_overview_database",
        "external_api_called": False,
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
