from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from services.backtest.errors import BacktestValidationError
from services.backtest.portfolio import OrderIntent
from services.backtest.validation import normalize_schedule


class CodeStrategy:
    key = ""
    version = "1"
    name = ""
    description = ""
    selection_modes: tuple[str, ...] = ()
    parameter_schema: dict[str, dict] = {}
    default_symbols: list[dict] = []

    def __init__(self, params: dict | None = None):
        self.params = self.validate_params(params or {})

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        result: dict[str, Any] = {}
        for name, spec in cls.parameter_schema.items():
            value = params.get(name, spec.get("default"))
            value_type = spec.get("type")
            try:
                if value_type == "number":
                    if isinstance(value, bool):
                        raise ValueError
                    value = float(value)
                elif value_type == "integer":
                    if isinstance(value, bool):
                        raise ValueError
                    converted = int(value)
                    if isinstance(value, float) and value != converted:
                        raise ValueError
                    value = converted
                elif value_type == "boolean":
                    if not isinstance(value, bool):
                        raise ValueError
                elif value_type == "time":
                    value = normalize_schedule(value)
                    if value in {"OPEN", "CLOSE"}:
                        raise BacktestValidationError(
                            f"参数 {name} 必须是具体的 HH:MM 时间。"
                        )
            except (TypeError, ValueError) as exc:
                raise BacktestValidationError(f"参数 {name} 类型不正确。") from exc
            if value_type in {"number", "integer"} and not math.isfinite(
                float(value)
            ):
                raise BacktestValidationError(f"参数 {name} 必须是有限数值。")
            if "minimum" in spec and value < spec["minimum"]:
                raise BacktestValidationError(f"参数 {name} 低于最小值。")
            if "maximum" in spec and value > spec["maximum"]:
                raise BacktestValidationError(f"参数 {name} 超过最大值。")
            result[name] = value
        unknown = set(params) - set(cls.parameter_schema)
        if unknown:
            raise BacktestValidationError(f"代码策略包含未知参数：{sorted(unknown)}。")
        return result

    @classmethod
    def catalog_item(cls) -> dict:
        return {
            "key": cls.key,
            "version": cls.version,
            "name": cls.name,
            "description": cls.description,
            "selection_modes": list(cls.selection_modes),
            "parameter_schema": cls.parameter_schema,
            "default_symbols": cls.default_symbols,
            "required_events": list(cls.required_events({})),
            "minimum_lookback": cls.minimum_lookback({}),
        }

    @classmethod
    def required_events(cls, params: dict) -> tuple[str, ...]:
        raise NotImplementedError

    @classmethod
    def minimum_lookback(cls, params: dict) -> int:
        raise NotImplementedError

    def on_event(self, context) -> list[OrderIntent]:
        raise NotImplementedError


class RapidDropAtrRotationStrategy(CodeStrategy):
    key = "rapid_drop_atr_rotation"
    version = "1.0.0"
    name = "急跌回避与 ATR 动量轮动"
    description = (
        "09:40 排除近三日出现单日急跌的标的，10:00 选择五日相对 ATR "
        "涨幅最高的标的并持有。"
    )
    selection_modes = ("competition",)
    default_symbols = [
        {"symbol": "SPY", "max_weight": 100},
        {"symbol": "GLD", "max_weight": 100},
        {"symbol": "NVDA", "max_weight": 100},
        {"symbol": "MU", "max_weight": 100},
        {"symbol": "XLE", "max_weight": 100},
    ]
    parameter_schema = {
        "drop_threshold_percent": {
            "label": "单日急跌阈值",
            "type": "number",
            "default": 5.0,
            "minimum": 0.1,
            "maximum": 50.0,
            "step": 0.1,
            "unit": "%",
        },
        "drop_lookback_sessions": {
            "label": "急跌观察交易日",
            "type": "integer",
            "default": 3,
            "minimum": 1,
            "maximum": 20,
        },
        "risk_check_time": {
            "label": "风险检查时间",
            "type": "time",
            "default": "09:40",
        },
        "selection_time": {
            "label": "轮动选标时间",
            "type": "time",
            "default": "10:00",
        },
        "momentum_lookback_sessions": {
            "label": "涨幅观察交易日",
            "type": "integer",
            "default": 5,
            "minimum": 2,
            "maximum": 100,
        },
        "atr_period": {
            "label": "ATR 周期",
            "type": "integer",
            "default": 5,
            "minimum": 2,
            "maximum": 100,
        },
        "target_weight": {
            "label": "胜出标的目标仓位",
            "type": "number",
            "default": 100.0,
            "minimum": 1.0,
            "maximum": 100.0,
            "unit": "%",
        },
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.risk_off: dict[str, set[str]] = {}

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        values = super().validate_params(params)
        if values["risk_check_time"] >= values["selection_time"]:
            raise BacktestValidationError("风险检查时间必须早于轮动选标时间。")
        return values

    @classmethod
    def required_events(cls, params: dict) -> tuple[str, ...]:
        values = cls.validate_params(params)
        return (values["risk_check_time"], values["selection_time"])

    @classmethod
    def minimum_lookback(cls, params: dict) -> int:
        values = cls.validate_params(params)
        return max(
            values["drop_lookback_sessions"] + 1,
            values["momentum_lookback_sessions"],
            values["atr_period"] + 1,
        )

    def on_event(self, context) -> list[OrderIntent]:
        trading_date = context.trading_date
        if context.event == self.params["risk_check_time"]:
            return self._risk_check(context)
        if context.event == self.params["selection_time"]:
            return self._select(context)
        return []

    def _risk_check(self, context) -> list[OrderIntent]:
        flagged: set[str] = set()
        threshold = -self.params["drop_threshold_percent"] / 100
        lookback = self.params["drop_lookback_sessions"]
        for symbol in context.universe:
            rows = context.dataset.daily_before(symbol, context.trading_date)
            event_price = context.event_prices[symbol].signal_price
            returns: list[float] = []
            completed_needed = max(0, lookback - 1)
            if completed_needed:
                selected = rows[-(completed_needed + 1):]
                returns.extend(
                    float(current["close"]) / float(previous["close"]) - 1
                    for previous, current in zip(selected, selected[1:])
                )
            returns.append(event_price / float(rows[-1]["close"]) - 1)
            if any(value <= threshold for value in returns[-lookback:]):
                flagged.add(symbol)
        self.risk_off[context.trading_date] = flagged
        return [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"09:40 急跌检查命中，{symbol} 当日回避",
            )
            for symbol in sorted(flagged)
            if context.portfolio.quantity(symbol) > 0
        ]

    def _select(self, context) -> list[OrderIntent]:
        flagged = self.risk_off.get(context.trading_date, set())
        scores: list[tuple[float, str]] = []
        momentum_lookback = self.params["momentum_lookback_sessions"]
        atr_period = self.params["atr_period"]
        for symbol in context.universe:
            if symbol in flagged:
                continue
            rows = context.dataset.daily_before(symbol, context.trading_date)
            base = float(rows[-momentum_lookback]["close"])
            current_price = context.event_prices[symbol].signal_price
            expression = context.expression_context(symbol)
            atr = expression.resolve_function("atr", atr_period)
            if atr > 0:
                scores.append(((current_price - base) / atr, symbol))
        winner = sorted(scores, key=lambda item: (-item[0], item[1]))[0][1] if scores else None
        intents = [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"10:00 轮动换仓，胜出标的为 {winner or '无'}",
            )
            for symbol in context.universe
            if symbol != winner and context.portfolio.quantity(symbol) > 0
        ]
        if winner:
            intents.append(
                OrderIntent(
                    symbol=winner,
                    action="BUY",
                    sizing_mode="TARGET",
                    value_percent=self.params["target_weight"],
                    reason=f"10:00 ATR 动量评分最高：{winner}",
                )
            )
        return intents


STRATEGY_REGISTRY: dict[str, type[CodeStrategy]] = {
    RapidDropAtrRotationStrategy.key: RapidDropAtrRotationStrategy,
}


def list_code_strategies() -> list[dict]:
    return [strategy.catalog_item() for strategy in STRATEGY_REGISTRY.values()]


def get_code_strategy(code_key: str) -> type[CodeStrategy]:
    try:
        return STRATEGY_REGISTRY[code_key]
    except KeyError as exc:
        raise BacktestValidationError(f"未知代码策略：{code_key}。") from exc
