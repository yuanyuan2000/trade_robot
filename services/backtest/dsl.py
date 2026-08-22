from __future__ import annotations

import ast
from dataclasses import dataclass
import math
import re
from typing import Callable

from services.backtest.errors import BacktestValidationError


ALLOWED_NAMES = {"price", "position", "true", "false"}
HISTORY_FUNCTIONS = {"open", "high", "low", "close", "volume"}
INDICATOR_FUNCTIONS = {
    "ma", "ema", "atr", "ratr", "wtme", "rapid_drop", "rsi",
    "macd_line", "macd_signal", "macd_hist",
}
ALLOWED_FUNCTIONS = HISTORY_FUNCTIONS | INDICATOR_FUNCTIONS
_EQUALITY = re.compile(r"(?<![<>=!])=(?!=)")
_BOOLEAN_WORD = re.compile(r"\b(AND|OR|NOT|TRUE|FALSE)\b", re.IGNORECASE)


def normalize_expression(expression: str) -> str:
    value = (expression or "").strip()
    if not value:
        return "true"
    replacements = {
        "AND": "and",
        "OR": "or",
        "NOT": "not",
        "TRUE": "true",
        "FALSE": "false",
    }
    value = _BOOLEAN_WORD.sub(
        lambda match: replacements[match.group(1).upper()],
        value,
    )
    return _EQUALITY.sub("==", value)


def _node_error(node: ast.AST, message: str) -> BacktestValidationError:
    return BacktestValidationError(
        f"规则公式不合法：{message}",
        detail={"node": type(node).__name__},
    )


def _numeric_constant(node: ast.AST) -> float | int | None:
    if (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and isinstance(node.value, (int, float))
        and math.isfinite(float(node.value))
    ):
        return node.value
    return None


def _validate_function_call(node: ast.Call) -> None:
    name = node.func.id.lower()
    if node.keywords:
        raise _node_error(node, "指标函数不支持命名参数。")
    expected = {
        "wtme": {2, 3},
        "rapid_drop": {2},
        "macd_line": {2},
        "macd_signal": {3},
        "macd_hist": {3},
    }.get(name, {1})
    if len(node.args) not in expected:
        signatures = {
            "wtme": "wtme(周期, 半衰期[, epsilon])",
            "rapid_drop": "rapid_drop(观察段数, 跌幅阈值%)",
            "macd_line": "macd_line(快线周期, 慢线周期)",
            "macd_signal": "macd_signal(快线周期, 慢线周期, 信号周期)",
            "macd_hist": "macd_hist(快线周期, 慢线周期, 信号周期)",
        }
        signature = signatures.get(name, f"{name}(周期)")
        raise _node_error(node, f"函数参数数量错误，应使用 {signature}。")

    period = _numeric_constant(node.args[0])
    if not isinstance(period, int) or not 1 <= period <= 500:
        raise _node_error(node, "周期 n 必须是 1 至 500 的整数，禁止使用 0。")
    if name.startswith("macd_"):
        periods = [_numeric_constant(argument) for argument in node.args]
        if not all(isinstance(value, int) and 1 <= value <= 500 for value in periods):
            raise _node_error(node, "MACD 各周期必须是 1 至 500 的整数。")
        if int(periods[0]) >= int(periods[1]):
            raise _node_error(node, "MACD 快线周期必须小于慢线周期。")
    elif name == "wtme":
        if period < 2:
            raise _node_error(node, "WTME 周期必须是 2 至 500 的整数。")
        half_life = _numeric_constant(node.args[1])
        if half_life is None or not 0.1 <= float(half_life) <= 500:
            raise _node_error(node, "WTME 半衰期必须在 0.1 至 500 之间。")
        if len(node.args) == 3:
            epsilon = _numeric_constant(node.args[2])
            if epsilon is None or not 1e-12 <= float(epsilon) <= 0.01:
                raise _node_error(node, "WTME epsilon 必须在 1e-12 至 0.01 之间。")
    elif name == "rapid_drop":
        threshold = _numeric_constant(node.args[1])
        if threshold is None or not 0.1 <= float(threshold) <= 50:
            raise _node_error(node, "急跌阈值必须在 0.1% 至 50% 之间。")


def _call_arguments(node: ast.Call) -> tuple[float | int, ...]:
    return tuple(_numeric_constant(argument) for argument in node.args)


def _format_argument(value: float | int) -> str:
    return str(value) if isinstance(value, int) else f"{float(value):g}"


def _call_lookback(node: ast.Call) -> int:
    name = node.func.id.lower()
    arguments = _call_arguments(node)
    period = int(arguments[0])
    if name == "macd_line":
        return int(arguments[1])
    if name in {"macd_signal", "macd_hist"}:
        return int(arguments[1]) + int(arguments[2]) - 1
    return period + 1 if name in {"atr", "ratr"} else period


def _validate_node(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return
        if isinstance(node.value, (int, float)) and math.isfinite(float(node.value)):
            return
        raise _node_error(node, "只允许有限数值和布尔值。")
    if isinstance(node, ast.Name):
        if node.id.lower() not in ALLOWED_NAMES:
            raise _node_error(node, f"不支持变量 {node.id}。")
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise _node_error(node, "只允许 AND、OR。")
        for value in node.values:
            _validate_node(value)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            raise _node_error(node, "不支持该一元运算。")
        _validate_node(node.operand)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            raise _node_error(node, "只允许 +、-、*、/。")
        _validate_node(node.left)
        _validate_node(node.right)
        return
    if isinstance(node, ast.Compare):
        if not all(
            isinstance(operator, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq))
            for operator in node.ops
        ):
            raise _node_error(node, "只允许 >、>=、<、<=、=、!=。")
        _validate_node(node.left)
        for comparator in node.comparators:
            _validate_node(comparator)
        return
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id.lower() not in ALLOWED_FUNCTIONS
        ):
            raise _node_error(node, "调用了不支持的函数。")
        _validate_function_call(node)
        return
    raise _node_error(node, "包含不支持的语法。")


@dataclass(frozen=True)
class CompiledExpression:
    source: str
    tree: ast.Expression
    max_lookback: int

    def evaluate(self, context) -> float | bool:
        value = _evaluate_node(self.tree.body, context)
        if isinstance(value, float) and not math.isfinite(value):
            raise BacktestValidationError("规则计算得到了非有限数值。")
        return value

    def resolve_inputs(self, context) -> dict[str, float]:
        """Return the concrete variables and indicator values used by a rule."""
        values: dict[str, float] = {}
        names = {
            node.id.lower()
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name)
        }
        if "price" in names:
            values["price"] = float(context.price)
        if "position" in names:
            values["position"] = float(context.position)
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id.lower()
            arguments = _call_arguments(node)
            key = f"{name}({','.join(_format_argument(value) for value in arguments)})"
            if key not in values:
                values[key] = float(context.resolve_function(name, *arguments))
        return values


def compile_expression(expression: str) -> CompiledExpression:
    normalized = normalize_expression(expression)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise BacktestValidationError(
            "规则公式语法错误。",
            detail={"offset": exc.offset, "text": exc.text},
        ) from exc
    _validate_node(tree)
    lookbacks = [
        _call_lookback(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    return CompiledExpression(
        source=normalized,
        tree=tree,
        max_lookback=max(lookbacks, default=0),
    )


def _evaluate_node(node: ast.AST, context):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        name = node.id.lower()
        if name == "price":
            return float(context.price)
        if name == "position":
            return float(context.position)
        if name == "true":
            return True
        if name == "false":
            return False
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for child in node.values:
                if not bool(_evaluate_node(child, context)):
                    return False
            return True
        for child in node.values:
            if bool(_evaluate_node(child, context)):
                return True
        return False
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not bool(value)
        if isinstance(node.op, ast.USub):
            return -float(value)
        return +float(value)
    if isinstance(node, ast.BinOp):
        left = float(_evaluate_node(node.left, context))
        right = float(_evaluate_node(node.right, context))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise BacktestValidationError("规则公式发生除以零。")
        return left / right
    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, context)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate_node(comparator, context)
            checks: list[tuple[type[ast.cmpop], Callable]] = [
                (ast.Gt, lambda: left > right),
                (ast.GtE, lambda: left >= right),
                (ast.Lt, lambda: left < right),
                (ast.LtE, lambda: left <= right),
                (ast.Eq, lambda: left == right),
                (ast.NotEq, lambda: left != right),
            ]
            passed = next(check() for kind, check in checks if isinstance(operator, kind))
            if not passed:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        name = node.func.id.lower()
        return float(context.resolve_function(name, *_call_arguments(node)))
    raise BacktestValidationError("规则公式包含无法执行的节点。")
