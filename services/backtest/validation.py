from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
import re
from typing import Any

from services.backtest.dsl import compile_expression
from services.backtest.errors import BacktestValidationError
from services.market_context import normalize_market_config


STRATEGY_MODES = {"visual", "code"}
SELECTION_MODES = {"single", "distribution", "competition"}
ACTIONS = {"BUY", "SELL", "HOLD"}
SIZING_MODES = {"DELTA", "TARGET"}
BENCHMARKS = {"none", "SPY", "GLD", "auto"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^./=_-]{1,24}$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_BACKTEST_SETTINGS = {
    "start_date": "2020-02-03",
    "end_date": (date.today() - timedelta(days=1)).isoformat(),
    "initial_capital": 100_000.0,
    "leverage_multiplier": 1.0,
    "commission_per_share": 0.01,
    "minimum_commission": 1.0,
    "slippage_bps": 0.0,
    "allow_fractional_shares": False,
    "benchmark": "auto",
    "risk_free_rate": 0.045,
    "strict_data": True,
    "generate_logs": True,
}


def default_backtest_settings() -> dict:
    return {
        **DEFAULT_BACKTEST_SETTINGS,
        "end_date": (date.today() - timedelta(days=1)).isoformat(),
    }


def normalize_symbol(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(value):
        raise BacktestValidationError(f"标的代码不合法：{value or '空值'}。")
    return value


def normalize_schedule(value: Any) -> str:
    schedule = str(value or "").strip().upper()
    if schedule in {"OPEN", "CLOSE"}:
        return schedule
    match = TIME_PATTERN.fullmatch(schedule)
    if not match:
        raise BacktestValidationError("执行时间必须为 OPEN、CLOSE 或 HH:MM。")
    minute = int(match.group(1)) * 60 + int(match.group(2))
    if not 9 * 60 + 30 <= minute < 16 * 60:
        raise BacktestValidationError("具体时间必须处于美股常规交易时段 09:30 至 15:59。")
    return schedule


def validate_settings(settings: dict | None) -> dict:
    value = {**default_backtest_settings(), **(settings or {})}
    try:
        start = date.fromisoformat(str(value["start_date"]))
        end = date.fromisoformat(str(value["end_date"]))
    except (TypeError, ValueError) as exc:
        raise BacktestValidationError("回测起止日期格式必须为 YYYY-MM-DD。") from exc
    if start > end:
        raise BacktestValidationError("回测开始日期不能晚于结束日期。")
    value["start_date"] = start.isoformat()
    value["end_date"] = end.isoformat()

    numeric_rules = (
        ("initial_capital", 0, None, False),
        ("leverage_multiplier", 1, 10, True),
        ("commission_per_share", 0, None, True),
        ("minimum_commission", 0, None, True),
        ("slippage_bps", 0, 1000, True),
        ("risk_free_rate", -1, 1, True),
    )
    for name, minimum, maximum, inclusive in numeric_rules:
        if isinstance(value[name], bool):
            raise BacktestValidationError(f"{name} 必须是数值，不能是布尔值。")
        try:
            number = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BacktestValidationError(f"{name} 必须是数值。") from exc
        if not math.isfinite(number):
            raise BacktestValidationError(f"{name} 必须是有限数值。")
        if (number < minimum if inclusive else number <= minimum):
            raise BacktestValidationError(f"{name} 超出允许范围。")
        if maximum is not None and number > maximum:
            raise BacktestValidationError(f"{name} 超出允许范围。")
        value[name] = number
    for name in ("allow_fractional_shares", "strict_data", "generate_logs"):
        if not isinstance(value[name], bool):
            raise BacktestValidationError(f"{name} 必须是布尔值。")
    if not value["strict_data"]:
        raise BacktestValidationError("历史回测必须启用严格数据校验。")
    value["benchmark"] = str(value["benchmark"]).strip()
    if value["benchmark"] not in BENCHMARKS:
        raise BacktestValidationError("比较基准必须为 none、SPY、GLD 或 auto。")
    return value


def _validate_symbols(definition: dict, selection_mode: str) -> list[dict]:
    raw_symbols = definition.get("symbols")
    if not isinstance(raw_symbols, list):
        raise BacktestValidationError("策略标的必须是数组。")
    symbols: list[dict] = []
    seen: set[str] = set()
    for raw in raw_symbols:
        if not isinstance(raw, dict):
            raise BacktestValidationError("标的配置格式错误。")
        symbol = normalize_symbol(raw.get("symbol"))
        if symbol in seen:
            raise BacktestValidationError(f"标的 {symbol} 重复。")
        seen.add(symbol)
        if isinstance(raw.get("max_weight", 100), bool):
            raise BacktestValidationError(f"{symbol} 最大仓位不能是布尔值。")
        try:
            maximum = float(raw.get("max_weight", 100))
        except (TypeError, ValueError) as exc:
            raise BacktestValidationError(f"{symbol} 最大仓位必须是数值。") from exc
        if not math.isfinite(maximum):
            raise BacktestValidationError(f"{symbol} 最大仓位必须是有限数值。")
        if not 0 < maximum <= 100:
            raise BacktestValidationError(f"{symbol} 最大仓位必须大于 0 且不超过 100%。")
        raw_leverage = raw.get("leverage_multiplier", 1)
        if isinstance(raw_leverage, bool):
            raise BacktestValidationError(f"{symbol} 单标的杠杆不能是布尔值。")
        try:
            leverage = float(raw_leverage)
        except (TypeError, ValueError) as exc:
            raise BacktestValidationError(f"{symbol} 单标的杠杆必须是数值。") from exc
        if not math.isfinite(leverage):
            raise BacktestValidationError(f"{symbol} 单标的杠杆必须是有限数值。")
        if not 1 <= leverage <= 10:
            raise BacktestValidationError(f"{symbol} 单标的杠杆必须在 1 至 10 倍之间。")
        symbols.append(
            {
                "symbol": symbol,
                "max_weight": maximum,
                "leverage_multiplier": leverage,
            }
        )
    if not symbols:
        raise BacktestValidationError("策略至少需要一个标的。")
    if selection_mode == "single" and len(symbols) != 1:
        raise BacktestValidationError("single 模式必须且只能选择一个标的。")
    if selection_mode == "distribution" and sum(
        item["max_weight"] for item in symbols
    ) > 100 + 1e-9:
        raise BacktestValidationError("distribution 模式各标的最大仓位之和不得超过 100%。")
    if selection_mode == "competition" and len(symbols) < 2:
        raise BacktestValidationError("competition 模式至少需要两个标的。")
    return symbols


def _validate_rules(definition: dict, *, require_enabled: bool = True) -> list[dict]:
    raw_rules = definition.get("rules", [])
    if not isinstance(raw_rules, list):
        raise BacktestValidationError("可视化策略规则必须是数组。")
    rules: list[dict] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise BacktestValidationError(f"第 {index + 1} 条规则格式错误。")
        rule_id = str(raw.get("id") or f"rule-{index + 1}").strip()
        if rule_id in ids:
            raise BacktestValidationError(f"规则 ID 重复：{rule_id}。")
        ids.add(rule_id)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BacktestValidationError(
                f"第 {index + 1} 条规则 enabled 必须是布尔值。"
            )
        action = str(raw.get("action", "")).upper()
        if action not in ACTIONS:
            raise BacktestValidationError(f"第 {index + 1} 条规则 action 不合法。")
        sizing_mode = str(raw.get("sizing_mode", "TARGET")).upper()
        value = raw.get("value", 0)
        if action == "HOLD":
            sizing_mode = "TARGET"
            value = 0.0
        else:
            if sizing_mode not in SIZING_MODES:
                raise BacktestValidationError(f"第 {index + 1} 条规则仓位模式不合法。")
            if isinstance(value, bool):
                raise BacktestValidationError(
                    f"第 {index + 1} 条规则仓位不能是布尔值。"
                )
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise BacktestValidationError(f"第 {index + 1} 条规则仓位必须是数值。") from exc
            if not math.isfinite(value):
                raise BacktestValidationError(
                    f"第 {index + 1} 条规则仓位必须是有限数值。"
                )
            if not 0 <= value <= 100:
                raise BacktestValidationError(f"第 {index + 1} 条规则仓位必须在 0% 至 100%。")
        condition = str(raw.get("condition") or "true").strip()
        compiled = compile_expression(condition)
        rules.append(
            {
                "id": rule_id,
                "name": str(raw.get("name") or f"规则 {index + 1}").strip(),
                "enabled": enabled,
                "priority": int(raw.get("priority", index)),
                "action": action,
                "sizing_mode": sizing_mode,
                "value": value,
                "condition": condition,
                "when": normalize_schedule(raw.get("when", "OPEN")),
                "_max_lookback": compiled.max_lookback,
            }
        )
    if require_enabled and not any(rule["enabled"] for rule in rules):
        raise BacktestValidationError("至少需要一条启用的规则。")
    return rules


def _schedule_minute(schedule: str) -> int:
    if schedule == "OPEN":
        return 9 * 60 + 30
    if schedule == "CLOSE":
        return 16 * 60
    hour, minute = schedule.split(":", 1)
    return int(hour) * 60 + int(minute)


def validate_strategy_payload(payload: dict, *, creating: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise BacktestValidationError("策略配置必须是 JSON 对象。")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 100:
        raise BacktestValidationError("策略名称不能为空且不能超过 100 个字符。")
    design_mode = str(payload.get("design_mode") or "").lower()
    selection_mode = str(payload.get("selection_mode") or "").lower()
    if design_mode not in STRATEGY_MODES:
        raise BacktestValidationError("策略设计模式必须为 visual 或 code。")
    if selection_mode not in SELECTION_MODES:
        raise BacktestValidationError("选标模式不合法。")
    definition = deepcopy(payload.get("definition") or {})
    definition["symbols"] = _validate_symbols(definition, selection_mode)

    code_key = payload.get("code_key")
    code_version = payload.get("code_version")
    if design_mode == "visual":
        if code_key:
            raise BacktestValidationError("非代码策略不能设置 code_key。")
        definition["rules"] = _validate_rules(
            definition,
            require_enabled=selection_mode != "competition",
        )
        if selection_mode == "competition":
            if any(
                rule["enabled"] and rule["action"] == "BUY"
                for rule in definition["rules"]
            ):
                raise BacktestValidationError(
                    "competition 模式的普通规则只用于风险退出，不能包含 BUY；买入由候选条件和评分公式决定。"
                )
            competition = deepcopy(definition.get("competition") or {})
            eligibility = str(competition.get("eligibility") or "true").strip()
            score = str(competition.get("score") or "").strip()
            if not score:
                raise BacktestValidationError("competition 模式必须设置评分公式。")
            compile_expression(eligibility)
            compile_expression(score)
            when = normalize_schedule(competition.get("when", "OPEN"))
            eligibility_when = normalize_schedule(
                competition.get("eligibility_when", when)
            )
            if _schedule_minute(eligibility_when) > _schedule_minute(when):
                raise BacktestValidationError(
                    "候选条件检查时间不能晚于竞争评分时间。"
                )
            raw_minimum_score = competition.get("minimum_score")
            if raw_minimum_score is None or raw_minimum_score == "":
                minimum_score = None
            else:
                if isinstance(raw_minimum_score, bool):
                    raise BacktestValidationError("最低可入选评分必须是数值。")
                try:
                    minimum_score = float(raw_minimum_score)
                except (TypeError, ValueError) as exc:
                    raise BacktestValidationError("最低可入选评分必须是数值。") from exc
                if not math.isfinite(minimum_score):
                    raise BacktestValidationError("最低可入选评分必须是有限数值。")
            raw_target_weight = competition.get("target_weight", 100)
            if isinstance(raw_target_weight, bool):
                raise BacktestValidationError("竞争胜出标的目标仓位不能是布尔值。")
            target_weight = float(raw_target_weight)
            if not math.isfinite(target_weight):
                raise BacktestValidationError("竞争胜出标的目标仓位必须是有限数值。")
            if not 0 < target_weight <= 100:
                raise BacktestValidationError("竞争胜出标的目标仓位必须大于 0 且不超过 100%。")
            limiting = [
                item
                for item in definition["symbols"]
                if item["max_weight"] + 1e-9 < target_weight
            ]
            if limiting:
                raise BacktestValidationError(
                    "竞争目标仓位不能超过任一候选标的的最大仓位。"
                )
            cash_when_none = competition.get("cash_when_none", True)
            if not isinstance(cash_when_none, bool):
                raise BacktestValidationError("cash_when_none 必须是布尔值。")
            rebalance_existing = competition.get("rebalance_existing", True)
            if not isinstance(rebalance_existing, bool):
                raise BacktestValidationError("rebalance_existing 必须是布尔值。")
            definition["competition"] = {
                "eligibility": eligibility,
                "score": score,
                "minimum_score": minimum_score,
                "target_weight": target_weight,
                "cash_when_none": cash_when_none,
                "rebalance_existing": rebalance_existing,
                "eligibility_when": eligibility_when,
                "when": when,
            }
        code_key = None
        code_version = None
    else:
        code_key = str(code_key or "").strip()
        if not code_key:
            raise BacktestValidationError("代码策略必须选择 code_key。")
        if "params" not in definition or not isinstance(definition["params"], dict):
            definition["params"] = {}
        code_version = str(code_version or "").strip() or None

    raw_schema_version = payload.get("schema_version", 1)
    if isinstance(raw_schema_version, bool):
        raise BacktestValidationError("策略 schema_version 不能是布尔值。")
    try:
        schema_version = int(raw_schema_version)
    except (TypeError, ValueError) as exc:
        raise BacktestValidationError("策略 schema_version 必须是整数。") from exc
    if isinstance(raw_schema_version, float) and raw_schema_version != schema_version:
        raise BacktestValidationError("策略 schema_version 必须是整数。")
    if schema_version != 1:
        raise BacktestValidationError(
            f"暂不支持策略 schema_version={schema_version}。"
        )
    return {
        "name": name,
        "description": str(payload.get("description") or "").strip() or None,
        "design_mode": design_mode,
        "selection_mode": selection_mode,
        "code_key": code_key,
        "code_version": code_version,
        "market": normalize_market_config(payload.get("market")),
        "definition": definition,
        "default_settings": validate_settings(payload.get("default_settings")),
        "schema_version": schema_version,
    }


def default_strategy_payload(
    *,
    name: str,
    design_mode: str,
    selection_mode: str,
    code_key: str | None = None,
) -> dict:
    count = 1 if selection_mode == "single" else 2
    default_weight = 50 if selection_mode == "distribution" else 100
    symbols = [
        {"symbol": symbol, "max_weight": default_weight, "leverage_multiplier": 1.0}
        for symbol in ["SPY", "GLD"][:count]
    ]
    standard_rules = [
        {
            "id": "buy-above-ma",
            "name": "价格高于20日均线时买入",
            "enabled": True,
            "priority": 10,
            "action": "BUY",
            "sizing_mode": "TARGET",
            "value": 100 if count == 1 else 50,
            "condition": "price > ma(20)",
            "when": "OPEN",
        },
        {
            "id": "sell-below-ma",
            "name": "价格低于20日均线时清仓",
            "enabled": True,
            "priority": 20,
            "action": "SELL",
            "sizing_mode": "TARGET",
            "value": 0,
            "condition": "price < ma(20)",
            "when": "OPEN",
        },
    ]
    if selection_mode == "competition":
        standard_rules = []
    definition: dict[str, Any] = {
        "symbols": symbols,
        "rules": standard_rules,
    }
    if selection_mode == "competition":
        definition["competition"] = {
            "eligibility": "price > ma(20)",
            "score": "(price - close(5)) / atr(5)",
            "minimum_score": None,
            "target_weight": 100,
            "cash_when_none": True,
            "rebalance_existing": True,
            "eligibility_when": "OPEN",
            "when": "OPEN",
        }
    if design_mode == "code":
        definition["params"] = {}
        definition.pop("rules", None)
    return validate_strategy_payload(
        {
            "name": name,
            "design_mode": design_mode,
            "selection_mode": selection_mode,
            "code_key": code_key,
            "definition": definition,
            "default_settings": default_backtest_settings(),
        },
        creating=True,
    )
