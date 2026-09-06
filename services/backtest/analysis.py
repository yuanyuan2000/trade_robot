from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date, timedelta
import math
import re
import threading

from database import repository
from services.backtest.data import _sha256
from services.backtest.errors import BacktestValidationError
from services.corporate_action_adjustment_service import adjust_price_rows


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ANALYSIS_PERIODS = (1, 3, 6, 12)
_CACHE_LIMIT = 24
_curve_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()


def _iso(value: object) -> str:
    return str(value or "")[:10]


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (following - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def _three_months_completed(first_date: str, latest_date: str) -> bool:
    start = date.fromisoformat(first_date)
    threshold = _add_months(start, 3) - timedelta(days=1)
    return date.fromisoformat(latest_date) >= threshold


def _snapshot_bounds(snapshot: dict) -> tuple[str | None, str | None]:
    points = snapshot.get("equity_points") or []
    if not points:
        return None, None
    return _iso(points[0].get("trading_date")), _iso(points[-1].get("trading_date"))


def _analysis_available(snapshot: dict) -> bool:
    first_date, latest_date = _snapshot_bounds(snapshot)
    if not first_date or not latest_date:
        return False
    status = str(snapshot.get("run", {}).get("status") or "")
    return status in TERMINAL_STATUSES or _three_months_completed(first_date, latest_date)


def _symbols(snapshot: dict) -> list[str]:
    definition = (
        snapshot.get("run", {})
        .get("strategy_snapshot", {})
        .get("definition", {})
    )
    return [
        str(item.get("symbol") or "").strip().upper()
        for item in definition.get("symbols", [])
        if str(item.get("symbol") or "").strip()
    ]


def _positive_leverage(value: object, default: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def _benchmark_leverage_assumptions(snapshot: dict) -> dict:
    run = snapshot.get("run", {})
    strategy = run.get("strategy_snapshot", {})
    definition = strategy.get("definition", {})
    overall = _positive_leverage(run.get("settings", {}).get("leverage_multiplier"))
    dynamic_symbol = bool(definition.get("dynamic_leverage_enabled", False))
    configured_symbol_multipliers = {
        str(item.get("symbol") or "").strip().upper(): _positive_leverage(
            item.get("leverage_multiplier")
        )
        for item in definition.get("symbols", [])
        if str(item.get("symbol") or "").strip()
    }
    # The comparison curve is a hypothetical fixed-multiplier buy-and-hold
    # series. A point-in-time dynamic multiplier is not fixed over the chosen
    # interval, so never substitute the disabled manual input here.
    symbol_multipliers = (
        {symbol: 1.0 for symbol in configured_symbol_multipliers}
        if dynamic_symbol
        else configured_symbol_multipliers
    )
    params = definition.get("params") or {}
    dynamic_special = (
        strategy.get("design_mode") == "code"
        and params.get("allocation_mode")
        in {"leveraged_equal", "leveraged_linear_rank"}
    )
    # There is currently no fixed special multiplier in shipped strategies.
    # Dynamic special leverage deliberately stays at one for this hypothetical
    # buy-and-hold comparison, as it cannot be known at the interval start.
    special = 1.0
    return {
        "overall_multiplier": overall,
        "symbol_multipliers": symbol_multipliers,
        "special_multiplier": special,
        "dynamic_symbol_assumed_one": dynamic_symbol,
        "dynamic_special_assumed_one": dynamic_special,
        "final_multipliers": {
            symbol: overall * multiplier * special
            for symbol, multiplier in symbol_multipliers.items()
        },
    }


def _leveraged_points(points: list[dict], multiplier: float) -> list[dict]:
    return [
        {
            **point,
            "return_rate": float(point["return_rate"]) * multiplier,
        }
        for point in points
    ]


def build_analysis_meta(snapshot: dict) -> dict:
    run = snapshot["run"]
    first_date, latest_date = _snapshot_bounds(snapshot)
    symbols = _symbols(snapshot)
    benchmark = str(run.get("settings", {}).get("benchmark") or "none")
    return {
        "run_id": run["id"],
        "status": run.get("status"),
        "strategy_name": run.get("strategy_name"),
        "market": run.get("strategy_snapshot", {}).get("market") or {},
        "available": _analysis_available(snapshot),
        "available_start_date": first_date,
        "available_end_date": latest_date,
        "periods": list(ANALYSIS_PERIODS),
        "symbols": symbols,
        "benchmark": benchmark,
        "benchmark_in_pool": benchmark.upper() in symbols,
        "auto_is_equal_weight": benchmark.lower() == "auto",
        "live_version": run.get("live", {}).get("version"),
    }


def _ensure_available(snapshot: dict) -> None:
    if not _analysis_available(snapshot):
        raise BacktestValidationError("回测尚未完成三个月，暂不能打开精细化分析。")


def _range(snapshot: dict, start_date: str, end_date: str) -> dict:
    _ensure_available(snapshot)
    try:
        requested_start = date.fromisoformat(_iso(start_date))
        requested_end = date.fromisoformat(_iso(end_date))
    except ValueError as exc:
        raise BacktestValidationError("分析区间日期格式必须为 YYYY-MM-DD。") from exc
    if requested_start > requested_end:
        raise BacktestValidationError("分析区间起点不能晚于终点。")
    if requested_end > _add_months(requested_start, 12):
        raise BacktestValidationError("单次精细化分析区间不能超过 12 个月。")
    points = snapshot.get("equity_points") or []
    available_dates = [_iso(point.get("trading_date")) for point in points]
    selected = [
        value for value in available_dates
        if requested_start.isoformat() <= value <= requested_end.isoformat()
    ]
    if not selected:
        raise BacktestValidationError("所选区间内没有已完成的回测交易日。")
    return {
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "actual_start_date": selected[0],
        "actual_end_date": selected[-1],
        "trading_dates": selected,
    }


def _manifest_actions(run: dict, symbol: str) -> list[dict]:
    return [
        dict(item)
        for item in (run.get("data_manifest") or {}).get("corporate_actions", [])
        if item.get("symbol") == symbol and item.get("affects_position", True)
    ]


def _daily_rows(run: dict, symbol: str) -> tuple[list[dict], str | None]:
    market_type = (
        run.get("strategy_snapshot", {}).get("market", {}).get("type")
        or "US_EQUITY"
    )
    rows = repository.get_strategy_daily_prices(
        symbol,
        market_type,
        include_metadata=True,
    )
    manifest_symbol = (run.get("data_manifest") or {}).get("symbols", {}).get(symbol, {})
    warning = None
    daily_first = manifest_symbol.get("daily_first")
    daily_last = manifest_symbol.get("daily_last")
    expected_hash = manifest_symbol.get("daily_sha256")
    if daily_first and daily_last and expected_hash:
        audit_rows = [
            {key: row.get(key) for key in ("date", "open", "high", "low", "close", "volume")}
            for row in rows
            if daily_first <= _iso(row.get("date")) <= daily_last
        ]
        if _sha256(audit_rows) != expected_hash:
            warning = f"{symbol} 当前本地日线与该次回测的数据指纹不一致。"
    return rows, warning


def _asset_curve(
    run: dict,
    symbol: str,
    trading_dates: list[str],
) -> tuple[list[dict], dict[str, float], str | None]:
    rows, warning = _daily_rows(run, symbol)
    if not rows:
        return [], {}, warning or f"{symbol} 没有可用日线。"
    adjusted = adjust_price_rows(rows, _manifest_actions(run, symbol), mode="all")
    close_by_date = {
        _iso(row.get("date")): float(row["close"])
        for row in adjusted
        if row.get("close") is not None and math.isfinite(float(row["close"]))
    }
    available = [value for value in trading_dates if value in close_by_date]
    if not available:
        return [], {}, warning or f"{symbol} 在所选区间没有可用日线。"
    base = close_by_date[available[0]]
    if base <= 0:
        return [], {}, warning or f"{symbol} 区间起点价格无效。"
    returns = {value: close_by_date[value] / base - 1 for value in available}
    points = [
        {"trading_date": value, "return_rate": returns[value]}
        for value in available
    ]
    return points, returns, warning


def _strategy_curve(snapshot: dict, range_value: dict) -> list[dict]:
    selected = set(range_value["trading_dates"])
    points = [
        point for point in snapshot.get("equity_points", [])
        if _iso(point.get("trading_date")) in selected
    ]
    if not points:
        return []
    base = float(points[0]["equity"])
    return [
        {
            "trading_date": _iso(point["trading_date"]),
            "return_rate": float(point["equity"]) / base - 1 if base else 0.0,
        }
        for point in points
    ]


def _interval_metrics(snapshot: dict, range_value: dict) -> dict:
    selected_dates = set(range_value["trading_dates"])
    points = [
        point for point in snapshot.get("equity_points", [])
        if _iso(point.get("trading_date")) in selected_dates
    ]
    equities = [float(point["equity"]) for point in points]
    peak = equities[0] if equities else 0.0
    maximum_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, equity / peak - 1)
    trades = [
        trade for trade in snapshot.get("trades", [])
        if range_value["actual_start_date"] <= _iso(trade.get("event_time")) <= range_value["actual_end_date"]
    ]
    realized = sum(
        float(trade.get("realized_pnl") or 0)
        for trade in trades if trade.get("side") == "SELL"
    )
    return {
        "return_rate": equities[-1] / equities[0] - 1 if len(equities) > 1 and equities[0] else 0.0,
        "max_drawdown": maximum_drawdown,
        "trade_count": len(trades),
        "realized_pnl": realized,
    }


def build_analysis(snapshot: dict, start_date: str, end_date: str) -> dict:
    range_value = _range(snapshot, start_date, end_date)
    run = snapshot["run"]
    version = run.get("live", {}).get("version") or run.get("completed_at") or run.get("current_time")
    cache_key = (
        int(run["id"]),
        range_value["actual_start_date"],
        range_value["actual_end_date"],
        version,
    )
    with _cache_lock:
        cached = _curve_cache.get(cache_key)
        if cached is not None:
            _curve_cache.move_to_end(cache_key)
            return deepcopy(cached)

    symbols = _symbols(snapshot)
    leverage = _benchmark_leverage_assumptions(snapshot)
    benchmark = str(run.get("settings", {}).get("benchmark") or "none")
    series = [{"key": "strategy", "label": "策略", "type": "strategy", "points": _strategy_curve(snapshot, range_value)}]
    asset_returns: dict[str, dict[str, float]] = {}
    warnings: list[str] = []
    for symbol in symbols:
        points, returns, warning = _asset_curve(run, symbol, range_value["trading_dates"])
        final_leverage = leverage["final_multipliers"].get(
            symbol,
            leverage["overall_multiplier"],
        )
        asset_returns[symbol] = returns
        series.append({
            "key": f"asset:{symbol}",
            "label": symbol,
            "type": "asset",
            "symbol": symbol,
            "configured_benchmark": (
                benchmark.upper() == symbol
                or (benchmark.lower() == "auto" and len(symbols) == 1)
            ),
            "points": points,
            "leveraged_points": _leveraged_points(points, final_leverage),
            "leverage_multiplier": final_leverage,
        })
        if warning:
            warnings.append(warning)

    if len(symbols) > 1:
        equal_points = []
        leveraged_equal_points = []
        last_values = {symbol: 0.0 for symbol in symbols}
        leveraged_last_values = {symbol: 0.0 for symbol in symbols}
        for trading_date in range_value["trading_dates"]:
            for symbol in symbols:
                if trading_date in asset_returns[symbol]:
                    last_values[symbol] = asset_returns[symbol][trading_date]
                    leveraged_last_values[symbol] = (
                        asset_returns[symbol][trading_date]
                        * leverage["final_multipliers"].get(
                            symbol,
                            leverage["overall_multiplier"],
                        )
                    )
            equal_points.append({
                "trading_date": trading_date,
                "return_rate": sum(last_values.values()) / len(symbols),
            })
            leveraged_equal_points.append({
                "trading_date": trading_date,
                "return_rate": sum(leveraged_last_values.values()) / len(symbols),
            })
        series.append({
            "key": "pool:equal",
            "label": "资产池等权",
            "type": "equal_weight",
            "configured_benchmark": benchmark.lower() == "auto",
            "points": equal_points,
            "leveraged_points": leveraged_equal_points,
            "leverage_mode": "per_asset",
        })

    normalized_benchmark = benchmark.upper()
    if benchmark.lower() not in {"none", "auto"} and normalized_benchmark not in symbols:
        points, _returns, warning = _asset_curve(run, normalized_benchmark, range_value["trading_dates"])
        final_leverage = leverage["overall_multiplier"]
        series.append({
            "key": f"benchmark:{normalized_benchmark}",
            "label": f"{normalized_benchmark}（配置基准）",
            "type": "benchmark",
            "symbol": normalized_benchmark,
            "configured_benchmark": True,
            "points": points,
            "leveraged_points": _leveraged_points(points, final_leverage),
            "leverage_multiplier": final_leverage,
        })
        if warning:
            warnings.append(warning)

    payload = {
        "range": range_value,
        "series": series,
        "metrics": _interval_metrics(snapshot, range_value),
        "benchmark_leverage": leverage,
        "warnings": list(dict.fromkeys(warnings)),
    }
    with _cache_lock:
        _curve_cache[cache_key] = deepcopy(payload)
        _curve_cache.move_to_end(cache_key)
        while len(_curve_cache) > _CACHE_LIMIT:
            _curve_cache.popitem(last=False)
    return payload


def _number(value: object, *, percent: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    if percent:
        return f"{number * 100:.4g}%"
    return f"{number:.4g}"


def _resolved_formula(formula: str | None, inputs: dict | None) -> str:
    value = str(formula or "—")
    for key in sorted((inputs or {}), key=len, reverse=True):
        value = re.sub(
            rf"(?<![\w]){re.escape(key)}(?![\w])",
            _number(inputs[key]),
            value,
            flags=re.IGNORECASE,
        )
    return value.replace("*", "×").replace("/", "÷").replace("==", "=")


def _code_formula(code_key: str, context: dict) -> tuple[str, str | None]:
    if code_key == "sevenstar_etf_rotation":
        return (
            f"{_number(context.get('annualized_returns'), percent=True)} × {_number(context.get('r_squared'))}",
            "评分 = 长期年化趋势 × R²",
        )
    if code_key == "rapid_drop_atr_rotation":
        return (
            f"({_number(context.get('current_price'))} - {_number(context.get('base_price'))}) ÷ {_number(context.get('atr'))}",
            "评分 =（当前价格 - 基准价格）÷ ATR",
        )
    if code_key == "rapid_drop_wtme_rotation":
        return (
            f"100 × {_number(context.get('weighted_return'))} ÷ {_number(context.get('weighted_true_range'))}",
            "评分 = 100 × 加权收益 ÷ 加权真实波幅",
        )
    if context.get("score_formula_display"):
        return str(context["score_formula_display"]), context.get("score_formula_help")
    return str(context.get("score_formula") or "—"), None


def build_decision(snapshot: dict, trading_date: str) -> dict:
    _ensure_available(snapshot)
    target_date = _iso(trading_date)
    first_date, latest_date = _snapshot_bounds(snapshot)
    if not first_date or not first_date <= target_date <= latest_date:
        raise BacktestValidationError("决策日期不在已完成回测区间内。")
    run = snapshot["run"]
    strategy = run.get("strategy_snapshot", {})
    definition = strategy.get("definition", {})
    dynamic_without_rebalance = bool(
        definition.get("dynamic_leverage_enabled")
        and not (
            run.get("settings", {})
            .get("dynamic_leverage", {})
            .get("rebalance_on_change", True)
        )
    )
    logs = [log for log in snapshot.get("logs", []) if _iso(log.get("event_time")) == target_date]
    trades = [trade for trade in snapshot.get("trades", []) if _iso(trade.get("event_time")) == target_date]
    if strategy.get("design_mode") == "code":
        accepted = {
            "RAPID_DROP_ATR_DAILY_SCORE",
            "RAPID_DROP_WTME_DAILY_SCORE",
            "SEVENSTAR_DAILY_SCORE",
        }
        rows = []
        help_text = None
        for log in logs:
            if log.get("event_type") not in accepted:
                continue
            context = log.get("context") or {}
            formula, row_help = _code_formula(str(strategy.get("code_key") or ""), context)
            help_text = help_text or row_help
            reasons = list(context.get("filter_reasons") or [])
            rows.append({
                "symbol": context.get("symbol") or context.get("etf") or log.get("symbol"),
                "filtered": not bool(context.get("eligible", not reasons)),
                "filter_reasons": reasons,
                "exposure_percent": context.get(
                    "exposure_percent", context.get("holding_percent", 0.0)
                ),
                "actual_exposure_percent": context.get(
                    "actual_exposure_percent",
                    context.get("actual_holding_percent", context.get("holding_percent", 0.0)),
                ),
                "calculated_exposure_percent": context.get(
                    "calculated_exposure_percent",
                    context.get("calculated_holding_percent", context.get("holding_percent", 0.0)),
                ),
                "score": context.get("score"),
                "formula": formula,
                "rank": context.get("rank"),
            })
        rows.sort(key=lambda item: (item["score"] is None, -(float(item["score"]) if item["score"] is not None else 0), str(item["symbol"])))
        return {
            "date": target_date,
            "mode": "competition",
            "rows": rows,
            "formula_help": help_text,
            "trades": trades,
            "show_calculated_and_actual_exposure": dynamic_without_rebalance,
        }

    if strategy.get("selection_mode") == "competition":
        by_symbol: dict[str, dict] = {}
        for log in logs:
            if log.get("event_type") not in {"COMPETITION_ELIGIBILITY", "COMPETITION_SCORE"}:
                continue
            symbol = str(log.get("symbol") or "")
            row = by_symbol.setdefault(symbol, {
                "symbol": symbol,
                "filtered": False,
                "filter_reasons": [],
                "exposure_percent": 0.0,
                "actual_exposure_percent": 0.0,
                "calculated_exposure_percent": 0.0,
                "score": None,
                "formula": "—",
                "rank": None,
            })
            context = log.get("context") or {}
            legacy_exposure = context.get("holding_percent")
            if context.get("exposure_percent") is not None:
                row["exposure_percent"] = context.get("exposure_percent")
            elif legacy_exposure is not None:
                row["exposure_percent"] = legacy_exposure
            if context.get("actual_exposure_percent") is not None:
                row["actual_exposure_percent"] = context.get(
                    "actual_exposure_percent"
                )
            elif context.get("actual_holding_percent") is not None:
                row["actual_exposure_percent"] = context.get("actual_holding_percent")
            elif legacy_exposure is not None:
                row["actual_exposure_percent"] = legacy_exposure
            if context.get("calculated_exposure_percent") is not None:
                row["calculated_exposure_percent"] = context.get(
                    "calculated_exposure_percent"
                )
            elif context.get("calculated_holding_percent") is not None:
                row["calculated_exposure_percent"] = context.get("calculated_holding_percent")
            elif legacy_exposure is not None:
                row["calculated_exposure_percent"] = legacy_exposure
            if log.get("event_type") == "COMPETITION_ELIGIBILITY":
                if not context.get("matched"):
                    row["filtered"] = True
                    row["filter_reasons"] = [context.get("reason") or "候选条件未通过"]
            else:
                row["score"] = context.get("score")
                row["formula"] = _resolved_formula(context.get("formula"), context.get("inputs"))
                if context.get("passes_minimum_score") is False:
                    row["filtered"] = True
                    row["filter_reasons"] = ["低于最低可入选评分"]
        rows = list(by_symbol.values())
        rows.sort(key=lambda item: (item["score"] is None, -(float(item["score"]) if item["score"] is not None else 0), item["symbol"]))
        return {
            "date": target_date,
            "mode": "competition",
            "rows": rows,
            "formula_help": "评分公式中的变量和指标均为该决策时点的实际值。",
            "trades": trades,
            "show_calculated_and_actual_exposure": dynamic_without_rebalance,
        }

    rule_map = {
        rule.get("id"): rule
        for rule in strategy.get("definition", {}).get("rules", [])
    }
    rows = []
    for log in logs:
        if log.get("event_type") != "RULE_EVALUATION":
            continue
        context = log.get("context") or {}
        rule = rule_map.get(context.get("rule_id"), {})
        content = (
            f"{rule.get('action', '')} {rule.get('sizing_mode', '')} {rule.get('value', '')}% "
            f"IF {context.get('condition') or rule.get('condition', '')} WHEN {rule.get('when', '')}"
        ).strip()
        rows.append({
            "symbol": log.get("symbol"),
            "rule_name": context.get("rule_name"),
            "content": content,
            "matched": bool(context.get("matched")),
            "resolved_condition": _resolved_formula(context.get("condition"), context.get("inputs")),
        })
    return {"date": target_date, "mode": "rules", "rows": rows, "trades": trades}


def build_candles(snapshot: dict, symbol: str, start_date: str, end_date: str) -> dict:
    range_value = _range(snapshot, start_date, end_date)
    normalized = str(symbol or "").strip().upper()
    if normalized not in _symbols(snapshot):
        raise BacktestValidationError("K线标的必须属于该次回测的资产池。")
    rows, warning = _daily_rows(snapshot["run"], normalized)
    selected_dates = set(range_value["trading_dates"])
    candles = [
        {key: row.get(key) for key in ("date", "open", "high", "low", "close", "volume")}
        for row in rows if _iso(row.get("date")) in selected_dates
    ]
    trades = [
        trade for trade in snapshot.get("trades", [])
        if trade.get("symbol") == normalized
        and range_value["actual_start_date"] <= _iso(trade.get("event_time")) <= range_value["actual_end_date"]
    ]
    sells = [trade for trade in trades if trade.get("side") == "SELL"]
    profitable = [trade for trade in sells if float(trade.get("realized_pnl") or 0) > 0]
    realized_pnl = sum(float(trade.get("realized_pnl") or 0) for trade in sells)
    commission = sum(float(trade.get("commission") or 0) for trade in trades)
    slippage = sum(float(trade.get("slippage_amount") or 0) for trade in trades)
    starting_point = next(
        (
            point for point in snapshot.get("equity_points", [])
            if _iso(point.get("trading_date")) == range_value["actual_start_date"]
        ),
        None,
    )
    starting_equity = float(starting_point.get("equity") or 0) if starting_point else 0.0
    net_realized_pnl = realized_pnl - commission - slippage
    return {
        "range": range_value,
        "symbol": normalized,
        "candles": candles,
        "trades": trades,
        "summary": {
            "buy_count": sum(1 for trade in trades if trade.get("side") == "BUY"),
            "sell_count": len(sells),
            "profitable_sell_count": len(profitable),
            "realized_pnl": realized_pnl,
            "commission": commission,
            "slippage": slippage,
            "net_realized_pnl": net_realized_pnl,
            "starting_equity": starting_equity,
            "return_rate": net_realized_pnl / starting_equity if starting_equity else None,
            "win_rate": len(profitable) / len(sells) if sells else None,
        },
        "warning": warning,
    }


def purge_analysis_cache(run_ids: list[int]) -> None:
    targets = {int(value) for value in run_ids}
    with _cache_lock:
        for key in list(_curve_cache):
            if int(key[0]) in targets:
                _curve_cache.pop(key, None)
