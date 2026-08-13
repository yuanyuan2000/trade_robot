from __future__ import annotations

import ast
import json
import re

from services.backtest.dsl import compile_expression
from services.backtest.errors import BacktestValidationError
from services.backtest.validation import normalize_schedule


PANEL_SCRIPT_VERSION = 1
MAX_COLUMNS = 12
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FORMATS = {"number", "price", "percent", "boolean"}


def _expression_calls(expression: str) -> list[str]:
    compiled = compile_expression(expression)
    calls: list[str] = []
    for node in ast.walk(compiled.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        arguments = ",".join(f"{float(arg.value):g}" for arg in node.args)
        value = f"{node.func.id.lower()}({arguments})"
        if value not in calls:
            calls.append(value)
    return calls


def _column_key(prefix: str, expression: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", expression.lower()).strip("_")
    return f"{prefix}_{clean}"[:40].rstrip("_")


def _indicator_column(expression: str) -> dict:
    name = expression.split("(", 1)[0].upper()
    return {
        "key": _column_key("indicator", expression),
        "label": expression.upper(),
        "expression": expression,
        "format": (
            "boolean" if name == "RAPID_DROP"
            else "price" if name in {"OPEN", "HIGH", "LOW", "CLOSE", "MA", "EMA", "ATR"}
            else "number"
        ),
        "help": f"策略公式使用的 {name} 历史值。",
    }


def generate_panel_settings(strategy: dict) -> dict:
    """Generate an editable, restricted JSON script for a visual strategy."""
    if strategy.get("design_mode") != "visual":
        return {
            "mode": "builtin",
            "script_version": PANEL_SCRIPT_VERSION,
            "customized": False,
        }
    definition = strategy.get("definition") or {}
    expressions: list[str] = []
    columns: list[dict] = []
    competition = definition.get("competition") or {}
    if strategy.get("selection_mode") == "competition":
        eligibility = str(competition.get("eligibility") or "true")
        score = str(competition.get("score") or "0")
        columns.extend([
            {
                "key": "eligible",
                "label": "候选条件",
                "expression": eligibility,
                "format": "boolean",
                "help": "是否满足策略的竞争候选条件。",
                "event": competition.get(
                    "eligibility_when", competition.get("when", "OPEN")
                ),
            },
            {
                "key": "score",
                "label": "评分",
                "expression": score,
                "format": "number",
                "help": "用于当前面板标的之间排序的策略评分。",
                "event": competition.get("when", "OPEN"),
            },
        ])
        expressions.extend((eligibility, score))
    for rule in definition.get("rules", []):
        if not rule.get("enabled", True):
            continue
        expression = str(rule.get("condition") or "true")
        expressions.append(expression)
    seen = {column["expression"] for column in columns}
    for expression in expressions:
        for call in _expression_calls(expression):
            if call in seen or len(columns) >= MAX_COLUMNS:
                continue
            seen.add(call)
            columns.append(_indicator_column(call))
    for index, rule in enumerate(definition.get("rules", []), start=1):
        if not rule.get("enabled", True) or len(columns) >= MAX_COLUMNS:
            continue
        expression = str(rule.get("condition") or "true")
        columns.append({
            "key": f"rule_{index}",
            "label": str(rule.get("name") or f"规则 {index}")[:24],
            "expression": expression,
            "format": "boolean",
            "help": f"当前价格下是否命中 {rule.get('action', 'HOLD')} 规则。",
            "event": rule.get("when", "OPEN"),
        })
    script = {
        "version": PANEL_SCRIPT_VERSION,
        "columns": columns[:MAX_COLUMNS],
        "default_sort": {
            "key": "score" if strategy.get("selection_mode") == "competition" else "symbol",
            "direction": "desc" if strategy.get("selection_mode") == "competition" else "asc",
        },
    }
    return {
        "mode": "restricted_json",
        "script_version": PANEL_SCRIPT_VERSION,
        "script": json.dumps(script, ensure_ascii=False, indent=2),
        "customized": False,
        "generated_from_strategy_revision": int(strategy.get("revision") or 1),
    }


def validate_panel_script(script_text: str) -> dict:
    if not isinstance(script_text, str) or not script_text.strip():
        raise BacktestValidationError("面板脚本不能为空。")
    try:
        payload = json.loads(script_text)
    except json.JSONDecodeError as exc:
        raise BacktestValidationError(
            f"面板脚本 JSON 格式错误（第 {exc.lineno} 行）。"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != PANEL_SCRIPT_VERSION:
        raise BacktestValidationError("面板脚本 version 必须为 1。")
    raw_columns = payload.get("columns")
    if not isinstance(raw_columns, list) or len(raw_columns) > MAX_COLUMNS:
        raise BacktestValidationError(f"面板列必须是数组且最多 {MAX_COLUMNS} 列。")
    columns = []
    keys: set[str] = set()
    for index, raw in enumerate(raw_columns, start=1):
        if not isinstance(raw, dict):
            raise BacktestValidationError(f"第 {index} 个面板列配置无效。")
        key = str(raw.get("key") or "").strip()
        if not _KEY_PATTERN.fullmatch(key) or key in keys:
            raise BacktestValidationError(f"第 {index} 个面板列 key 无效或重复。")
        keys.add(key)
        label = str(raw.get("label") or "").strip()
        help_text = str(raw.get("help") or "").strip()
        expression = str(raw.get("expression") or "").strip()
        value_format = str(raw.get("format") or "number").strip().lower()
        if not label or len(label) > 24:
            raise BacktestValidationError(f"第 {index} 个面板列标题不能为空且最多 24 字。")
        if len(help_text) > 120:
            raise BacktestValidationError(f"第 {index} 个面板列说明最多 120 字。")
        if value_format not in _FORMATS:
            raise BacktestValidationError(f"第 {index} 个面板列 format 不支持。")
        compiled = compile_expression(expression)
        event = raw.get("event")
        columns.append({
            "key": key,
            "label": label,
            "expression": compiled.source,
            "format": value_format,
            "help": help_text,
            "max_lookback": compiled.max_lookback,
            **({"event": normalize_schedule(event)} if event else {}),
        })
    sort = payload.get("default_sort") or {}
    sort_key = str(sort.get("key") or "symbol")
    if sort_key not in {"symbol", "latest_price", *keys}:
        raise BacktestValidationError("默认排序列不存在。")
    direction = str(sort.get("direction") or "asc").lower()
    if direction not in {"asc", "desc"}:
        raise BacktestValidationError("默认排序方向必须为 asc 或 desc。")
    return {
        "version": PANEL_SCRIPT_VERSION,
        "columns": columns,
        "default_sort": {"key": sort_key, "direction": direction},
    }


def validate_panel_settings(panel_settings: dict, strategy: dict) -> dict:
    if strategy.get("design_mode") != "visual":
        return generate_panel_settings(strategy)
    if not isinstance(panel_settings, dict):
        raise BacktestValidationError("面板设置必须是对象。")
    parsed = validate_panel_script(str(panel_settings.get("script") or ""))
    return {
        **panel_settings,
        "mode": "restricted_json",
        "script_version": PANEL_SCRIPT_VERSION,
        "script": json.dumps(
            {
                "version": parsed["version"],
                "columns": [
                    {
                        key: column[key]
                        for key in ("key", "label", "expression", "format", "help", "event")
                        if key in column
                    }
                    for column in parsed["columns"]
                ],
                "default_sort": parsed["default_sort"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "customized": bool(panel_settings.get("customized", True)),
    }
