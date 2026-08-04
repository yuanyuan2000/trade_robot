from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from services.backtest.errors import BacktestDataError, BacktestValidationError
from services.backtest.portfolio import OrderIntent
from services.backtest.validation import normalize_schedule, normalize_symbol
from services.indicator_service import calculate_wtme_components


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
                elif value_type == "symbol":
                    value = normalize_symbol(value)
                elif value_type == "choice":
                    if not isinstance(value, str):
                        raise ValueError
                    value = value.strip().lower()
                    allowed = {
                        str(option["value"]).strip().lower()
                        if isinstance(option, dict)
                        else str(option).strip().lower()
                        for option in spec.get("options", [])
                    }
                    if value not in allowed:
                        raise BacktestValidationError(
                            f"参数 {name} 取值不支持。"
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

    @classmethod
    def additional_symbols(cls, params: dict) -> tuple[str, ...]:
        return ()

    @classmethod
    def early_close_offsets(cls, params: dict) -> dict[str, int]:
        return {}

    @classmethod
    def cumulative_volume_events(cls, params: dict) -> tuple[str, ...]:
        return ()

    @classmethod
    def validate_definition(cls, definition: dict) -> None:
        return None

    def on_event(self, context) -> list[OrderIntent]:
        raise NotImplementedError

    def describe_run(self, definition: dict) -> str:
        """Return the immutable one-line strategy description stored with a run."""
        values = []
        for name, value in self.params.items():
            label = self.parameter_schema.get(name, {}).get("label", name)
            if isinstance(value, bool):
                value = "开启" if value else "关闭"
            values.append(f"{label}={value}")
        return f"{self.name}：{'，'.join(values)}"

    @classmethod
    def realtime_notification_intro(cls) -> str:
        return f"{cls.name}实时评分结果（仅供人工核验）："


class RapidDropAtrRotationStrategy(CodeStrategy):
    key = "rapid_drop_atr_rotation"
    version = "1.3.0"
    name = "急跌回避与 ATR 动量轮动"
    description = (
        "风险检查时按可独立启用的百分比/ATR 单日急跌规则过滤标的，"
        "轮动时选择相对 ATR 动量最高的前 N 只并等权持有。"
    )
    selection_modes = ("competition",)
    default_symbols = [
        {"symbol": "SPY", "max_weight": 100, "leverage_multiplier": 1},
        {"symbol": "GLD", "max_weight": 100, "leverage_multiplier": 1},
        {"symbol": "NVDA", "max_weight": 100, "leverage_multiplier": 1},
        {"symbol": "MU", "max_weight": 100, "leverage_multiplier": 1},
        {"symbol": "XLE", "max_weight": 100, "leverage_multiplier": 1},
    ]
    parameter_schema = {
        "holdings_num": {
            "label": "目标持仓数量",
            "type": "integer",
            "default": 1,
            "minimum": 1,
            "maximum": 100,
            "step": 1,
            "unit": "只",
            "help": "按评分取前 N 只；所有目标持仓平均分配总目标仓位。",
        },
        "enable_percent_drop_filter": {
            "label": "启用百分比急跌过滤",
            "type": "boolean",
            "default": True,
        },
        "drop_threshold_percent": {
            "label": "单日急跌百分比阈值",
            "type": "number",
            "default": 5.0,
            "minimum": 0.1,
            "maximum": 50.0,
            "step": 0.1,
            "unit": "%",
        },
        "enable_atr_drop_filter": {
            "label": "启用 ATR 急跌过滤",
            "type": "boolean",
            "default": False,
        },
        "drop_threshold_atr": {
            "label": "单日急跌 ATR 倍数",
            "type": "number",
            "default": 2.0,
            "minimum": 0.1,
            "maximum": 20.0,
            "step": 0.1,
            "unit": "ATR",
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
        "atr_weighting": {
            "label": "ATR 加权方式",
            "type": "choice",
            "default": "wilder",
            "options": [
                {"value": "wilder", "label": "Wilder（兼容现有结果）"},
                {"value": "ema", "label": "EMA（推荐，响应更快）"},
                {"value": "linear", "label": "线性加权（固定窗口）"},
                {"value": "simple", "label": "简单平均（等权）"},
            ],
            "help": "决定真实波幅 TR 的时间权重；评分和 ATR 急跌过滤使用同一口径。",
            "suggestion": "轮动策略推荐 EMA；Wilder 最平滑并保持旧回测结果。",
        },
        "target_weight": {
            "label": "入选标的总目标仓位",
            "type": "number",
            "default": 100.0,
            "minimum": 1.0,
            "maximum": 100.0,
            "unit": "%",
        },
    }

    @classmethod
    def realtime_notification_intro(cls) -> str:
        return "急跌回避 + ATR 动量轮动：按 ATR 动量评分排名，列出硬性过滤与最终目标。"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.risk_off: dict[str, set[str]] = {}
        self.risk_evaluations: dict[str, dict[str, dict]] = {}

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        values = super().validate_params(params)
        if values["risk_check_time"] >= values["selection_time"]:
            raise BacktestValidationError("风险检查时间必须早于轮动选标时间。")
        return values

    @classmethod
    def validate_definition(cls, definition: dict) -> None:
        values = cls.validate_params(definition.get("params", {}))
        candidates = [item["symbol"] for item in definition.get("symbols", [])]
        if values["holdings_num"] > len(candidates):
            raise BacktestValidationError("目标持仓数量不能超过候选池标的数量。")
        per_symbol_weight = values["target_weight"] / values["holdings_num"]
        if any(
            float(item.get("max_weight", 100)) + 1e-9 < per_symbol_weight
            for item in definition.get("symbols", [])
        ):
            raise BacktestValidationError(
                "候选标的最大仓位不能低于总目标仓位除以目标持仓数量。"
            )

    @classmethod
    def required_events(cls, params: dict) -> tuple[str, ...]:
        values = cls.validate_params(params)
        return (values["risk_check_time"], values["selection_time"])

    @classmethod
    def minimum_lookback(cls, params: dict) -> int:
        values = cls.validate_params(params)
        drop_requirement = 1
        if values["enable_percent_drop_filter"]:
            drop_requirement = values["drop_lookback_sessions"] + 1
        if values["enable_atr_drop_filter"]:
            drop_requirement = max(
                drop_requirement,
                values["drop_lookback_sessions"] + values["atr_period"] + 1,
            )
        return max(
            drop_requirement,
            values["momentum_lookback_sessions"],
            values["atr_period"] + 1,
        )

    def describe_run(self, definition: dict) -> str:
        filters = []
        if self.params["enable_percent_drop_filter"]:
            filters.append(f"{self.params['drop_threshold_percent']:g}%急跌")
        if self.params["enable_atr_drop_filter"]:
            filters.append(
                f"{self.params['drop_threshold_atr']:g}倍ATR急跌"
            )
        return (
            f"{self.params['risk_check_time']}检查"
            f"{self.params['drop_lookback_sessions']}日{'/'.join(filters) or '无急跌过滤'}，"
            f"{self.params['selection_time']}按"
            f"{self.params['momentum_lookback_sessions']}日ATR动量选择前"
            f"{self.params['holdings_num']}只，总目标仓位"
            f"{self.params['target_weight']:g}%（目标不变时不重复再平衡）"
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
        lookback = self.params["drop_lookback_sessions"]
        evaluations: dict[str, dict] = {}
        for symbol in context.universe:
            rows = context.dataset.daily_before(symbol, context.trading_date)
            event_price = context.event_prices[symbol].signal_price
            start = len(rows) - lookback
            previous_rows = rows[start:]
            current_prices = [
                *[float(row["close"]) for row in previous_rows[1:]],
                event_price,
            ]
            percent_change_details = [
                {
                    "from_date": previous["date"],
                    "to_date": (
                        previous_rows[index + 1]["date"]
                        if index + 1 < len(previous_rows)
                        else context.trading_date + " " + self.params["risk_check_time"]
                    ),
                    "previous_close": float(previous["close"]),
                    "current_price": current_price,
                    "change": current_price / float(previous["close"]) - 1,
                    "formula": f"{current_price:.6f} / {float(previous['close']):.6f} - 1",
                }
                for index, (previous, current_price) in enumerate(zip(previous_rows, current_prices))
            ]
            percent_changes = [item["change"] for item in percent_change_details]
            atr_changes: list[float] = []
            atr_change_details: list[dict] = []
            if self.params["enable_atr_drop_filter"]:
                atr_series = self._atr_series(
                    rows,
                    self.params["atr_period"],
                    self.params["atr_weighting"],
                )
                for row_index, (previous, current_price) in enumerate(
                    zip(previous_rows, current_prices), start=start
                ):
                    atr_value = atr_series[row_index]
                    if atr_value is None or atr_value <= 0:
                        continue
                    change = (current_price - float(previous["close"])) / atr_value
                    atr_changes.append(change)
                    atr_change_details.append({
                        "from_date": previous["date"],
                        "previous_close": float(previous["close"]),
                        "current_price": current_price,
                        "atr": atr_value,
                        "change_atr": change,
                        "formula": f"({current_price:.6f} - {float(previous['close']):.6f}) / {atr_value:.6f}",
                    })
            filter_codes: list[str] = []
            filter_reasons: list[str] = []
            if (
                self.params["enable_percent_drop_filter"]
                and any(
                    value <= -self.params["drop_threshold_percent"] / 100
                    for value in percent_changes
                )
            ):
                filter_codes.append("percent_drop")
                filter_reasons.append("百分比单日急跌")
            if (
                self.params["enable_atr_drop_filter"]
                and any(
                    value <= -self.params["drop_threshold_atr"]
                    for value in atr_changes
                )
            ):
                filter_codes.append("atr_drop")
                filter_reasons.append("ATR 单日急跌")
            evaluations[symbol] = {
                "filter_codes": filter_codes,
                "filter_reasons": filter_reasons,
                "percent_changes": percent_changes,
                "percent_change_details": percent_change_details,
                "atr_changes": atr_changes,
                "atr_change_details": atr_change_details,
                "risk_event_price": event_price,
                "percent_threshold": -self.params["drop_threshold_percent"] / 100,
                "atr_threshold": -self.params["drop_threshold_atr"],
            }
            if filter_codes:
                flagged.add(symbol)
            result_label = "过滤：" + "、".join(filter_reasons) if filter_reasons else "通过"
            worst_percent = min(percent_changes, default=0.0)
            worst_atr = min(atr_changes, default=0.0)
            context.log_custom(
                "RAPID_DROP_ATR_RISK_CHECK",
                f"{symbol} 风险检查{result_label}；最差单日涨跌 {worst_percent:.2%}"
                + (f"，最差 ATR 变化 {worst_atr:.4f}" if atr_changes else "")
                + "。",
                symbol=symbol,
                context=evaluations[symbol],
            )
        self.risk_off[context.trading_date] = flagged
        self.risk_evaluations[context.trading_date] = evaluations
        return [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"{self.params['risk_check_time']} 急跌检查命中，{symbol} 当日回避",
            )
            for symbol in sorted(flagged)
            if context.portfolio.quantity(symbol) > 0
        ]

    @staticmethod
    def _atr_series(
        rows: list[dict],
        period: int,
        weighting: str,
    ) -> list[float | None]:
        """Return non-lookahead ATR values ending at each completed daily bar."""
        result: list[float | None] = [None] * len(rows)
        true_ranges: list[float] = []
        for previous, current in zip(rows, rows[1:]):
            high = float(current["high"])
            low = float(current["low"])
            previous_close = float(previous["close"])
            true_ranges.append(
                max(high - low, abs(high - previous_close), abs(low - previous_close))
            )
        if len(true_ranges) < period:
            return result
        if weighting in {"simple", "linear"}:
            linear_denominator = period * (period + 1) / 2
            for row_index in range(period, len(rows)):
                window = true_ranges[row_index - period:row_index]
                if weighting == "simple":
                    result[row_index] = sum(window) / period
                else:
                    result[row_index] = sum(
                        weight * true_range
                        for weight, true_range in enumerate(window, start=1)
                    ) / linear_denominator
            return result
        atr = sum(true_ranges[:period]) / period
        result[period] = atr
        alpha = 1 / period if weighting == "wilder" else 2 / (period + 1)
        for row_index, true_range in enumerate(true_ranges[period:], start=period + 1):
            atr = alpha * true_range + (1 - alpha) * atr
            result[row_index] = atr
        return result

    @classmethod
    def _atr_value(cls, rows: list[dict], period: int, weighting: str) -> float:
        value = cls._atr_series(rows, period, weighting)[-1]
        if value is None:
            raise BacktestDataError("没有足够数据计算策略 ATR。")
        return float(value)

    def _select(self, context) -> list[OrderIntent]:
        flagged = self.risk_off.get(context.trading_date, set())
        scores: list[tuple[float, str]] = []
        evaluations: list[dict] = []
        momentum_lookback = self.params["momentum_lookback_sessions"]
        atr_period = self.params["atr_period"]
        for symbol in context.universe:
            rows = context.dataset.daily_before(symbol, context.trading_date)
            base = float(rows[-momentum_lookback]["close"])
            base_date = rows[-momentum_lookback]["date"]
            current_price = context.event_prices[symbol].signal_price
            atr = self._atr_value(rows, atr_period, self.params["atr_weighting"])
            score = None
            if atr > 0:
                score = (current_price - base) / atr
                if symbol not in flagged:
                    scores.append((score, symbol))
            risk = self.risk_evaluations.get(context.trading_date, {}).get(symbol, {})
            evaluations.append(
                {
                    "symbol": symbol,
                    "score": score,
                    "score_formula": (
                        f"({current_price:.6f} - {base:.6f}) / {atr:.6f}; "
                        f"[P({self.params['selection_time']}) - Close({base_date})] / "
                        f"ATR({atr_period}, {self.params['atr_weighting']})"
                    ),
                    "current_price": current_price,
                    "base_price": base,
                    "base_date": base_date,
                    "atr": atr,
                    "atr_weighting": self.params["atr_weighting"],
                    "filter_codes": list(risk.get("filter_codes", [])),
                    "filter_reasons": list(risk.get("filter_reasons", [])),
                    "percent_changes": list(risk.get("percent_changes", [])),
                    "atr_changes": list(risk.get("atr_changes", [])),
                    "risk_event_price": risk.get("risk_event_price"),
                }
            )
        ranked_scores = sorted(scores, key=lambda item: (-item[0], item[1]))
        targets = [
            symbol
            for _score, symbol in ranked_scores[: self.params["holdings_num"]]
        ]
        rank_by_symbol = {
            symbol: index + 1
            for index, (_score, symbol) in enumerate(ranked_scores)
        }
        for item in evaluations:
            item["rank"] = rank_by_symbol.get(item["symbol"])
            item["selected_for_target"] = item["symbol"] in targets
            filter_text = (
                f"，硬性过滤：{'；'.join(item['filter_reasons'])}"
                if item["filter_reasons"]
                else "，通过硬性过滤"
            )
            rank_text = f"，合格排名第 {item['rank']}" if item["rank"] else ""
            score_text = f"{item['score']:.8f}" if item["score"] is not None else "不可计算"
            context.log_custom(
                "RAPID_DROP_ATR_DAILY_SCORE",
                f"{item['symbol']} ATR 动量评分 {score_text}{rank_text}{filter_text}。",
                symbol=item["symbol"],
                context=item,
            )
        target_set = set(targets)
        target_label = "、".join(targets) or "无"
        target_percent = self.params["target_weight"] / self.params["holdings_num"]
        intents = [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"{self.params['selection_time']} 轮动换仓，目标标的为 {target_label}",
            )
            for symbol in context.universe
            if symbol not in target_set and context.portfolio.quantity(symbol) > 0
        ]
        for symbol in targets:
            if context.portfolio.quantity(symbol) > 0:
                continue
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    action="BUY",
                    sizing_mode="TARGET",
                    value_percent=target_percent,
                    reason=(
                        f"{self.params['selection_time']} ATR 动量前 "
                        f"{self.params['holdings_num']} 名等权：{symbol}"
                    ),
                )
            )
        return intents


class RapidDropWtmeRotationStrategy(CodeStrategy):
    key = "rapid_drop_wtme_rotation"
    version = "1.1.0"
    name = "急跌回避与 WTME 动量轮动"
    description = (
        "先按百分比单日急跌规则过滤标的，再以指定时点价格构造"
        "不含未来数据的当前观测，买入 WTME 评分最高的未过滤标的。"
    )
    selection_modes = ("competition",)
    default_symbols = [dict(item) for item in RapidDropAtrRotationStrategy.default_symbols]
    parameter_schema = {
        "wtme_period": {
            "label": "WTME 窗口 N",
            "type": "integer",
            "default": 40,
            "minimum": 2,
            "maximum": 500,
            "step": 1,
            "unit": "个收益观测",
            "help": "评分包含当前决策价形成的观测，因此需要此前至少 N 根完整日线。",
        },
        "wtme_half_life": {
            "label": "WTME 权重半衰期 h",
            "type": "number",
            "default": 15.0,
            "minimum": 0.1,
            "maximum": 500.0,
            "step": 0.1,
            "unit": "交易日",
            "help": "距离当前 h 个观测的原始权重为最新观测的一半。",
        },
        "wtme_epsilon": {
            "label": "WTME epsilon",
            "type": "number",
            "default": 1e-8,
            "minimum": 1e-12,
            "maximum": 0.01,
            "step": 1e-12,
            "help": "加入加权标准化真实波幅分母，防止零波动时除零。",
        },
        "enable_percent_drop_filter": RapidDropAtrRotationStrategy.parameter_schema[
            "enable_percent_drop_filter"
        ],
        "drop_threshold_percent": RapidDropAtrRotationStrategy.parameter_schema[
            "drop_threshold_percent"
        ],
        "drop_lookback_sessions": RapidDropAtrRotationStrategy.parameter_schema[
            "drop_lookback_sessions"
        ],
        "risk_check_time": RapidDropAtrRotationStrategy.parameter_schema[
            "risk_check_time"
        ],
        "selection_time": RapidDropAtrRotationStrategy.parameter_schema[
            "selection_time"
        ],
        "target_weight": RapidDropAtrRotationStrategy.parameter_schema["target_weight"],
    }

    @classmethod
    def realtime_notification_intro(cls) -> str:
        return "急跌回避 + WTME 轮动：列出硬性过滤、WTME 评分与最终目标。"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.risk_off: dict[str, set[str]] = {}
        self.risk_evaluations: dict[str, dict[str, dict]] = {}

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
    def validate_definition(cls, definition: dict) -> None:
        values = cls.validate_params(definition.get("params", {}))
        if any(
            float(item.get("max_weight", 100)) + 1e-9 < values["target_weight"]
            for item in definition.get("symbols", [])
        ):
            raise BacktestValidationError(
                "候选标的最大仓位不能低于 WTME 策略总目标仓位。"
            )

    @classmethod
    def minimum_lookback(cls, params: dict) -> int:
        values = cls.validate_params(params)
        drop_requirement = (
            values["drop_lookback_sessions"]
            if values["enable_percent_drop_filter"]
            else 1
        )
        return max(
            drop_requirement,
            values["wtme_period"],
        )

    def describe_run(self, definition: dict) -> str:
        filters = []
        if self.params["enable_percent_drop_filter"]:
            filters.append(f"{self.params['drop_threshold_percent']:g}%急跌")
        return (
            f"{self.params['risk_check_time']}检查"
            f"{self.params['drop_lookback_sessions']}日{'/'.join(filters) or '无急跌过滤'}，"
            f"{self.params['selection_time']}按 WTME("
            f"N={self.params['wtme_period']}, h={self.params['wtme_half_life']:g}, "
            f"epsilon={self.params['wtme_epsilon']:g}) 选择最高分标的，"
            f"目标仓位{self.params['target_weight']:g}%（目标不变时不重复再平衡）"
        )

    def on_event(self, context) -> list[OrderIntent]:
        if context.event == self.params["risk_check_time"]:
            return self._risk_check(context)
        if context.event == self.params["selection_time"]:
            return self._select(context)
        return []

    def _risk_check(self, context) -> list[OrderIntent]:
        flagged: set[str] = set()
        evaluations: dict[str, dict] = {}
        lookback = self.params["drop_lookback_sessions"]
        threshold = -self.params["drop_threshold_percent"] / 100
        for symbol in context.universe:
            rows = context.dataset.daily_before(symbol, context.trading_date)
            previous_rows = rows[-lookback:]
            event_price = context.event_prices[symbol].signal_price
            current_prices = [
                *[float(row["close"]) for row in previous_rows[1:]],
                event_price,
            ]
            change_details = [
                {
                    "from_date": previous["date"],
                    "to_date": (
                        previous_rows[index + 1]["date"]
                        if index + 1 < len(previous_rows)
                        else context.trading_date + " " + self.params["risk_check_time"]
                    ),
                    "previous_close": float(previous["close"]),
                    "current_price": current_price,
                    "change": current_price / float(previous["close"]) - 1,
                    "formula": (
                        f"{current_price:.6f} / "
                        f"{float(previous['close']):.6f} - 1"
                    ),
                }
                for index, (previous, current_price) in enumerate(
                    zip(previous_rows, current_prices)
                )
            ]
            changes = [item["change"] for item in change_details]
            triggered = (
                self.params["enable_percent_drop_filter"]
                and any(value <= threshold for value in changes)
            )
            filter_codes = ["percent_drop"] if triggered else []
            filter_reasons = ["百分比单日急跌"] if triggered else []
            evaluations[symbol] = {
                "filter_codes": filter_codes,
                "filter_reasons": filter_reasons,
                "percent_changes": changes,
                "percent_change_details": change_details,
                "risk_event_price": event_price,
                "percent_threshold": threshold,
            }
            if triggered:
                flagged.add(symbol)
            result_label = "过滤：百分比单日急跌" if triggered else "通过"
            context.log_custom(
                "RAPID_DROP_WTME_RISK_CHECK",
                f"{symbol} 风险检查{result_label}；"
                f"最差单日涨跌 {min(changes, default=0.0):.2%}。",
                symbol=symbol,
                context=evaluations[symbol],
            )
        self.risk_off[context.trading_date] = flagged
        self.risk_evaluations[context.trading_date] = evaluations
        return [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=(
                    f"{self.params['risk_check_time']} 百分比急跌检查命中，"
                    f"{symbol} 当日回避"
                ),
            )
            for symbol in sorted(flagged)
            if context.portfolio.quantity(symbol) > 0
        ]

    def _select(self, context) -> list[OrderIntent]:
        flagged = self.risk_off.get(context.trading_date, set())
        period = self.params["wtme_period"]
        half_life = self.params["wtme_half_life"]
        epsilon = self.params["wtme_epsilon"]
        scores: list[tuple[float, str]] = []
        evaluations: list[dict] = []
        for symbol in context.universe:
            completed_rows = context.dataset.daily_before(
                symbol, context.trading_date
            )
            previous_close = float(completed_rows[-1]["close"])
            current_price = context.event_prices[symbol].signal_price
            # The current daily bar is not complete at an intraday decision.
            # Treat its known price path as previous close -> event price.  This
            # gives a point-in-time TR lower bound without leaking the day's
            # eventual high/low into the score.
            current_observation = {
                "date": context.trading_date,
                "open": previous_close,
                "high": max(previous_close, current_price),
                "low": min(previous_close, current_price),
                "close": current_price,
            }
            score_rows = [*completed_rows[-period:], current_observation]
            components = calculate_wtme_components(
                score_rows,
                period,
                half_life,
                epsilon,
            )
            score = float(components["value"]) if components is not None else None
            if score is not None and symbol not in flagged:
                scores.append((score, symbol))
            risk = self.risk_evaluations.get(context.trading_date, {}).get(symbol, {})
            evaluations.append({
                "symbol": symbol,
                "score": score,
                "weighted_return": (
                    components["weighted_return"] if components is not None else None
                ),
                "weighted_true_range": (
                    components["weighted_true_range"] if components is not None else None
                ),
                "score_formula": (
                    f"100 × Rw / (Aw + {epsilon:g}); WTME(N={period}, "
                    f"h={half_life:g}) at {self.params['selection_time']}"
                ),
                "current_price": current_price,
                "previous_close": previous_close,
                "current_observation_true_range": abs(current_price - previous_close),
                "current_observation_is_partial": True,
                "filter_codes": list(risk.get("filter_codes", [])),
                "filter_reasons": list(risk.get("filter_reasons", [])),
                "percent_changes": list(risk.get("percent_changes", [])),
                "risk_event_price": risk.get("risk_event_price"),
            })

        ranked_scores = sorted(scores, key=lambda item: (-item[0], item[1]))
        target = ranked_scores[0][1] if ranked_scores else None
        rank_by_symbol = {
            symbol: index + 1
            for index, (_score, symbol) in enumerate(ranked_scores)
        }
        for item in evaluations:
            item["rank"] = rank_by_symbol.get(item["symbol"])
            item["selected_for_target"] = item["symbol"] == target
            filter_text = (
                f"，硬性过滤：{'；'.join(item['filter_reasons'])}"
                if item["filter_reasons"]
                else "，通过硬性过滤"
            )
            rank_text = f"，合格排名第 {item['rank']}" if item["rank"] else ""
            score_text = (
                f"{item['score']:.8f}" if item["score"] is not None else "不可计算"
            )
            context.log_custom(
                "RAPID_DROP_WTME_DAILY_SCORE",
                f"{item['symbol']} WTME 评分 {score_text}{rank_text}{filter_text}。",
                symbol=item["symbol"],
                context=item,
            )

        target_label = target or "无"
        intents = [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"{self.params['selection_time']} WTME 轮动换仓，目标标的为 {target_label}",
            )
            for symbol in context.universe
            if symbol != target and context.portfolio.quantity(symbol) > 0
        ]
        if target is not None and context.portfolio.quantity(target) <= 0:
            intents.append(
                OrderIntent(
                    symbol=target,
                    action="BUY",
                    sizing_mode="TARGET",
                    value_percent=self.params["target_weight"],
                    reason=(
                        f"{self.params['selection_time']} 未过滤标的 WTME 最高："
                        f"{target}"
                    ),
                )
            )
        return intents


class SevenStarEtfRotationStrategy(CodeStrategy):
    key = "sevenstar_etf_rotation"
    version = "1.1.0"
    name = "七星 ETF 轮动"
    description = (
        "以加权对数回归年化趋势乘可选 R² 口径排名；默认使用一致加权 R²，"
        "也可切换历史 v1.0.0 兼容公式。叠加高点回撤保护、"
        "非正长期趋势、放量过热、短期动量和近三日急跌过滤；先卖后买，"
        "无候选时转入 BIL。"
    )
    selection_modes = ("competition",)
    default_symbols = [
        {"symbol": symbol, "max_weight": 100, "leverage_multiplier": 1}
        for symbol in ("GLD", "USO", "SPY", "QQQ", "DIA", "IWM", "TLT")
    ]
    parameter_schema = {
        "trend_formula_mode": {
            "label": "长期趋势公式",
            "type": "choice",
            "default": "consistent_w2",
            "options": [
                {"value": "consistent_w2", "label": "一致加权 R²（推荐）"},
                {"value": "legacy_v1", "label": "历史 v1.0.0 不一致权重"},
            ],
            "help": (
                "一致模式用 q=w² 统一回归与 R²；历史模式复现旧版 w² 拟合、"
                "w 平方和及普通均值，可能产生负 R² 和双负正评分。"
            ),
            "suggestion": "默认使用一致模式；仅为复现或对照旧结果时选择历史模式。",
        },
        "lookback_days": {
            "label": "长期趋势回看",
            "type": "integer", "default": 25, "minimum": 5, "maximum": 250,
            "unit": "交易日", "step": 1,
            "help": "使用此前 N 个完整日线收盘价加当前事件价，共 N+1 点拟合趋势。",
            "suggestion": "默认 25；越小越灵敏，越大越稳定。",
        },
        "holdings_num": {
            "label": "目标持仓数量",
            "type": "integer", "default": 1, "minimum": 1, "maximum": 5,
            "unit": "只", "step": 1,
            "help": "按排名取前 N 只并等权；不能超过候选池数量。",
            "suggestion": "原策略默认 1；提高可分散风险但会稀释最强趋势。",
        },
        "min_score_threshold": {
            "label": "最低趋势得分",
            "type": "number", "default": 0.0, "minimum": 0.0, "maximum": 1000.0,
            "step": 0.01,
            "help": "仅保留 score 严格大于该值的标的。",
            "suggestion": "默认 0，保持原策略边界语义。",
        },
        "max_score_threshold": {
            "label": "最高趋势得分",
            "type": "number", "default": 100.0, "minimum": 0.01, "maximum": 1000.0,
            "step": 0.01,
            "help": "仅保留 score 严格小于该值的标的，用于排除异常极值。",
            "suggestion": "默认 100；必须大于最低得分。",
        },
        "rebalance_tolerance_percent": {
            "label": "调仓容差",
            "type": "number", "default": 5.0, "minimum": 0.0, "maximum": 25.0,
            "unit": "%", "step": 0.1,
            "help": "目标金额偏差严格超过该比例时才调整；空仓始终允许买入。",
            "suggestion": "默认 5%，可减少小额反复交易。",
        },
        "minimum_trade_value_usd": {
            "label": "最小非清仓交易额",
            "type": "number", "default": 0.0, "minimum": 0.0, "maximum": 100000.0,
            "unit": "USD", "step": 1.0,
            "help": "小于该金额的加仓或部分减仓跳过；完整清仓不受限制。",
            "suggestion": "默认 0；如真实账户会忽略小单，可按实际金额设置。",
        },
        "enable_profit_protection": {
            "label": "启用高点回撤保护", "type": "boolean", "default": True,
            "help": "当前价低于此前高点阈值时卖出并排除；与持仓成本无关。",
            "suggestion": "建议保持开启，这一语义完全沿用原策略。",
        },
        "profit_lookback_days": {
            "label": "高点回看周期",
            "type": "integer", "default": 1, "minimum": 1, "maximum": 20,
            "unit": "交易日", "step": 1,
            "help": "取此前 N 个完整交易日最高价的最大值。",
            "suggestion": "默认 1；增大会让保护参考更久以前的高点。",
        },
        "profit_drawdown_percent": {
            "label": "高点回撤阈值",
            "type": "number", "default": 5.0, "minimum": 0.1, "maximum": 50.0,
            "unit": "%", "step": 0.1,
            "help": "当前价小于等于历史最高价 × (1-阈值) 时触发。",
            "suggestion": "默认 5%；越小退出越敏感。",
        },
        "profit_check_time": {
            "label": "盘中保护检查", "type": "time", "default": "11:00",
            "help": "独立检查全部策略持仓并立即清仓触发标的。",
            "suggestion": "须早于卖出排名时间，提前收市日也必须可执行。",
        },
        "enable_volume_check": {
            "label": "启用放量过热过滤", "type": "boolean", "default": True,
            "help": "排名时比较当日截至决策前一分钟的累计量与历史完整日均量。",
            "suggestion": "建议开启以复刻原策略。",
        },
        "volume_lookback_days": {
            "label": "历史均量周期",
            "type": "integer", "default": 5, "minimum": 1, "maximum": 60,
            "unit": "交易日", "step": 1,
            "help": "计算此前 N 个完整交易日的平均成交量。",
            "suggestion": "默认 5。",
        },
        "volume_ratio_threshold": {
            "label": "放量倍数阈值",
            "type": "number", "default": 2.0, "minimum": 0.1, "maximum": 20.0,
            "unit": "倍", "step": 0.1,
            "help": "累计量/历史均量严格大于该值，且趋势年化也过热时才排除。",
            "suggestion": "默认 2 倍。",
        },
        "volume_return_limit_percent": {
            "label": "放量过滤年化门槛",
            "type": "number", "default": 100.0, "minimum": 0.0, "maximum": 2000.0,
            "unit": "%", "step": 1.0,
            "help": "拟合年化收益严格大于此值时，放量过滤才生效。",
            "suggestion": "默认 100%，对应原策略的 1.0。",
        },
        "enable_short_momentum_filter": {
            "label": "启用短期动量过滤", "type": "boolean", "default": True,
            "help": "短周期简单收益年化低于阈值时排除。",
            "suggestion": "建议开启以复刻原策略。",
        },
        "short_lookback_days": {
            "label": "短期动量周期",
            "type": "integer", "default": 10, "minimum": 2, "maximum": 120,
            "unit": "交易日", "step": 1,
            "help": "当前事件价相对 N 个完整交易日前收盘价计算年化。",
            "suggestion": "默认 10。",
        },
        "short_momentum_threshold_percent": {
            "label": "短期动量下限",
            "type": "number", "default": 0.0, "minimum": -99.0, "maximum": 2000.0,
            "unit": "%", "step": 0.1,
            "help": "短期年化严格低于该值时排除。",
            "suggestion": "默认 0%，仅保留短期非负趋势。",
        },
        "single_day_loss_percent": {
            "label": "近三日单日急跌阈值",
            "type": "number", "default": 3.0, "minimum": 0.1, "maximum": 30.0,
            "unit": "%", "step": 0.1,
            "help": "含当日事件收益在内的最近三段中，任一跌幅严格超过阈值即排除。",
            "suggestion": "默认 3%，对应原策略 loss=0.97。",
        },
        "sell_time": {
            "label": "排名与卖出时间", "type": "time", "default": "14:00",
            "help": "只在此时计算并缓存当日排名，然后先卖出非目标持仓。",
            "suggestion": "默认 14:00；提前收市日自动映射到收市前 2 分钟。",
        },
        "buy_time": {
            "label": "买入与再平衡时间", "type": "time", "default": "14:01",
            "help": "复用当日排名，重新检查高点回撤后再买入或等权再平衡。",
            "suggestion": "必须至少晚于卖出时间 1 分钟；提前收市日映射到收市前 1 分钟。",
        },
        "defensive_symbol": {
            "label": "防御标的", "type": "symbol", "default": "BIL",
            "help": "没有合格趋势标的时持有；不得与候选池重复。",
            "suggestion": "默认 BIL（1–3 月美国国债 ETF）。",
        },
    }

    @classmethod
    def realtime_notification_intro(cls) -> str:
        return "七星 ETF 趋势轮动：按加权趋势评分排名，列出过滤原因与最终目标/防御标的。"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.rankings_cache: dict[str, list[dict]] = {}

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        values = super().validate_params(params)
        if values["min_score_threshold"] >= values["max_score_threshold"]:
            raise BacktestValidationError("最低趋势得分必须严格小于最高趋势得分。")
        if cls._minutes(values["profit_check_time"]) > 12 * 60 + 55:
            raise BacktestValidationError("盘中保护检查时间不得晚于 12:55，以兼容提前收市日。")
        if cls._minutes(values["profit_check_time"]) >= cls._minutes(values["sell_time"]):
            raise BacktestValidationError("盘中保护检查时间必须早于排名与卖出时间。")
        if cls._minutes(values["buy_time"]) <= cls._minutes(values["sell_time"]):
            raise BacktestValidationError("买入时间必须至少晚于卖出时间 1 分钟。")
        return values

    @classmethod
    def required_events(cls, params: dict) -> tuple[str, ...]:
        values = cls.validate_params(params)
        events = [values["sell_time"], values["buy_time"]]
        if values["enable_profit_protection"]:
            events.append(values["profit_check_time"])
        return tuple(dict.fromkeys(events))

    @classmethod
    def minimum_lookback(cls, params: dict) -> int:
        values = cls.validate_params(params)
        return max(
            values["lookback_days"], values["short_lookback_days"],
            values["profit_lookback_days"], values["volume_lookback_days"], 3,
        )

    @classmethod
    def additional_symbols(cls, params: dict) -> tuple[str, ...]:
        return (cls.validate_params(params)["defensive_symbol"],)

    @classmethod
    def early_close_offsets(cls, params: dict) -> dict[str, int]:
        values = cls.validate_params(params)
        return {values["sell_time"]: 2, values["buy_time"]: 1}

    @classmethod
    def cumulative_volume_events(cls, params: dict) -> tuple[str, ...]:
        values = cls.validate_params(params)
        return (values["sell_time"],) if values["enable_volume_check"] else ()

    @classmethod
    def validate_definition(cls, definition: dict) -> None:
        values = cls.validate_params(definition.get("params", {}))
        candidates = [item["symbol"] for item in definition.get("symbols", [])]
        if values["holdings_num"] > len(candidates):
            raise BacktestValidationError("目标持仓数量不能超过候选池标的数量。")
        if values["defensive_symbol"] in candidates:
            raise BacktestValidationError("防御标的不得与候选池标的重复。")
        if any(
            float(item.get("max_weight", 100)) != 100
            for item in definition.get("symbols", [])
        ):
            raise BacktestValidationError("七星策略候选池的单标的最大仓位固定为 100%。")

    def describe_run(self, definition: dict) -> str:
        return (
            f"七星使用{self.params['lookback_days']}日长期趋势、"
            f"{self.params['short_lookback_days']}日短动量，"
            f"{self.params['sell_time']}排名卖出、{self.params['buy_time']}买入，"
            f"持有前{self.params['holdings_num']}只，无候选时转入"
            f"{self.params['defensive_symbol']}"
        )

    def on_event(self, context) -> list[OrderIntent]:
        if (
            self.params["enable_profit_protection"]
            and context.event == self.params["profit_check_time"]
        ):
            return self._profit_protection_intents(context)
        if context.event == self.params["sell_time"]:
            rankings = self._rank(context)
            self.rankings_cache[context.trading_date] = rankings
            return self._sell_intents(context, rankings)
        if context.event == self.params["buy_time"]:
            rankings = self.rankings_cache.get(context.trading_date, [])
            return self._buy_intents(context, rankings)
        return []

    def _profit_triggered(self, context, symbol: str) -> bool:
        if not self.params["enable_profit_protection"]:
            return False
        rows = context.dataset.daily_before(symbol, context.trading_date)
        lookback = self.params["profit_lookback_days"]
        if len(rows) < lookback or symbol not in context.event_prices:
            return False
        max_high = max(float(row["high"]) for row in rows[-lookback:])
        current = context.event_prices[symbol].signal_price
        return current <= max_high * (1 - self.params["profit_drawdown_percent"] / 100)

    def _profit_protection_intents(self, context) -> list[OrderIntent]:
        defensive = self.params["defensive_symbol"]
        held = [*context.universe, defensive]
        return [
            OrderIntent(
                symbol=symbol, action="SELL", sizing_mode="TARGET", value_percent=0,
                reason=f"高点回撤保护触发：{symbol}",
            )
            for symbol in held
            if context.portfolio.quantity(symbol) > 0
            and self._profit_triggered(context, symbol)
        ]

    @staticmethod
    def _weighted_trend(prices: list[float], lookback: int) -> tuple[float, float, float]:
        if lookback < 1 or len(prices) < lookback + 1:
            raise BacktestDataError("七星长期趋势回归没有足够的价格数据。")
        recent = np.asarray(prices[-(lookback + 1):], dtype=float)
        if recent.ndim != 1 or not np.all(np.isfinite(recent)) or np.any(recent <= 0):
            raise BacktestDataError("七星长期趋势回归要求全部价格为有限正数。")
        y = np.log(recent)
        x = np.arange(len(y), dtype=float)
        fit_weights = np.linspace(1.0, 2.0, len(y))
        importance = fit_weights ** 2
        weighted_mean = float(np.average(y, weights=importance))
        centered_y = y - weighted_mean
        ss_tot = float(np.sum(importance * centered_y ** 2))
        weighted_variance = ss_tot / float(np.sum(importance))
        if not math.isfinite(weighted_variance):
            raise BacktestDataError("七星长期趋势回归产生了非有限方差。")
        if weighted_variance <= np.finfo(float).eps:
            return 0.0, 0.0, 0.0

        # np.polyfit minimizes sum((w * residual) ** 2), so its effective
        # observation importance is q = w ** 2. Centering by the same q keeps
        # the slope unchanged while avoiding cancellation for nearly flat data.
        slope, centered_intercept = np.polyfit(
            x, centered_y, 1, w=fit_weights
        )
        slope = float(slope)
        centered_intercept = float(centered_intercept)
        if not math.isfinite(slope) or not math.isfinite(centered_intercept):
            raise BacktestDataError("七星长期趋势回归产生了非有限拟合结果。")
        annualized_exponent = slope * 250
        max_float = float(np.finfo(float).max)
        annualized = (
            max_float
            if annualized_exponent > math.log(max_float)
            else math.expm1(annualized_exponent)
        )
        fitted_centered = slope * x + centered_intercept
        ss_res = float(np.sum(importance * (centered_y - fitted_centered) ** 2))
        if not all(math.isfinite(value) for value in (annualized, ss_res, ss_tot)):
            raise BacktestDataError("七星长期趋势评分产生了非有限数值。")
        raw_r_squared = 1 - ss_res / ss_tot
        r_squared_tolerance = 1e-12
        if not math.isfinite(raw_r_squared) or not (
            -r_squared_tolerance <= raw_r_squared <= 1 + r_squared_tolerance
        ):
            # A coherent intercept regression cannot leave [0, 1]
            # mathematically. Fail this observation closed if numerical input
            # ever violates that invariant, rather than promoting a bad score.
            return annualized, 0.0, 0.0
        r_squared = min(1.0, max(0.0, raw_r_squared))
        score = annualized * r_squared
        if not math.isfinite(score):
            raise BacktestDataError("七星长期趋势评分产生了非有限数值。")
        return annualized, r_squared, score

    @staticmethod
    def _legacy_weighted_trend(
        prices: list[float], lookback: int
    ) -> tuple[float, float, float]:
        """Reproduce the v1.0.0 mixed-weight formula with finite-value guards."""
        if lookback < 1 or len(prices) < lookback + 1:
            raise BacktestDataError("七星长期趋势回归没有足够的价格数据。")
        recent = np.asarray(prices[-(lookback + 1):], dtype=float)
        if recent.ndim != 1 or not np.all(np.isfinite(recent)) or np.any(recent <= 0):
            raise BacktestDataError("七星长期趋势回归要求全部价格为有限正数。")
        y = np.log(recent)
        x = np.arange(len(y), dtype=float)
        weights = np.linspace(1.0, 2.0, len(y))
        slope, intercept = np.polyfit(x, y, 1, w=weights)
        slope = float(slope)
        intercept = float(intercept)
        if not math.isfinite(slope) or not math.isfinite(intercept):
            raise BacktestDataError("七星历史长期趋势回归产生了非有限拟合结果。")
        annualized_exponent = slope * 250
        max_float = float(np.finfo(float).max)
        annualized = (
            max_float
            if annualized_exponent >= math.log(max_float)
            else math.exp(annualized_exponent) - 1
        )
        fitted = slope * x + intercept
        ss_res = float(np.sum(weights * (y - fitted) ** 2))
        ss_tot = float(np.sum(weights * (y - float(np.mean(y))) ** 2))
        if not all(math.isfinite(value) for value in (annualized, ss_res, ss_tot)):
            raise BacktestDataError("七星历史长期趋势评分产生了非有限数值。")
        if ss_tot == 0:
            return annualized, 0.0, 0.0
        r_squared = 1 - ss_res / ss_tot
        if not math.isfinite(r_squared):
            return annualized, 0.0, 0.0
        score = annualized * r_squared
        if not math.isfinite(score):
            score_sign = -1.0 if (annualized < 0) != (r_squared < 0) else 1.0
            score = math.copysign(max_float, score_sign)
        return annualized, r_squared, score

    def _metrics(self, context, symbol: str) -> dict:
        rows = context.dataset.daily_before(symbol, context.trading_date)
        current = context.event_prices[symbol].signal_price
        prices = [float(row["close"]) for row in rows] + [current]
        filter_codes = []
        filter_reasons = []
        if self._profit_triggered(context, symbol):
            filter_codes.append("high_drawdown")
            filter_reasons.append("高点回撤保护")
        formula_mode = self.params["trend_formula_mode"]
        trend_function = (
            self._legacy_weighted_trend
            if formula_mode == "legacy_v1"
            else self._weighted_trend
        )
        annualized, r_squared, score = trend_function(
            prices, self.params["lookback_days"]
        )
        volume_ratio = None
        if self.params["enable_volume_check"]:
            history = rows[-self.params["volume_lookback_days"]:]
            average = sum(float(row.get("volume") or 0) for row in history) / len(history)
            current_volume = context.dataset.cumulative_volume(
                symbol, context.trading_date, self.params["sell_time"]
            )
            volume_ratio = current_volume / average if average > 0 else 0.0
            if (
                volume_ratio > self.params["volume_ratio_threshold"]
                and annualized > self.params["volume_return_limit_percent"] / 100
            ):
                filter_codes.append("volume_overheat")
                filter_reasons.append("放量过热")
        short_days = self.params["short_lookback_days"]
        short_return = current / prices[-(short_days + 1)] - 1
        short_annualized = (1 + short_return) ** (250 / short_days) - 1
        if (
            self.params["enable_short_momentum_filter"]
            and short_annualized < self.params["short_momentum_threshold_percent"] / 100
        ):
            filter_codes.append("short_momentum")
            filter_reasons.append("短期动量不足")
        loss_factor = 1 - self.params["single_day_loss_percent"] / 100
        recent_ratios = (
            prices[-1] / prices[-2],
            prices[-2] / prices[-3],
            prices[-3] / prices[-4],
        )
        recent_min_ratio = min(recent_ratios)
        if recent_min_ratio < loss_factor:
            filter_codes.append("single_day_loss")
            filter_reasons.append("近三段单日急跌")
        if formula_mode == "consistent_w2" and annualized <= 0:
            filter_codes.append("non_positive_trend")
            filter_reasons.append("长期拟合趋势非正")
        if not self.params["min_score_threshold"] < score < self.params["max_score_threshold"]:
            filter_codes.append("score_range")
            filter_reasons.append("趋势评分超出开区间")
        return {
            "etf": symbol, "annualized_returns": annualized,
            "r_squared": r_squared, "score": score,
            "trend_formula_mode": formula_mode,
            "current_price": current, "short_annualized": short_annualized,
            "volume_ratio": volume_ratio,
            "recent_min_ratio": recent_min_ratio,
            "eligible": not filter_codes,
            "filter_codes": filter_codes,
            "filter_reasons": filter_reasons,
        }

    def _rank(self, context) -> list[dict]:
        evaluations = [self._metrics(context, symbol) for symbol in context.universe]
        ranked = [item for item in evaluations if item["eligible"]]
        ranked.sort(key=lambda item: item["score"], reverse=True)
        rank_by_symbol = {
            item["etf"]: index + 1
            for index, item in enumerate(ranked)
        }
        selected = {
            item["etf"]
            for item in ranked[: self.params["holdings_num"]]
        }
        for item in evaluations:
            item["rank"] = rank_by_symbol.get(item["etf"])
            item["selected_for_target"] = item["etf"] in selected
        context.log_strategy_evaluations(evaluations)
        return ranked

    def _targets(self, context, rankings: list[dict], *, recheck: bool) -> list[str]:
        result = []
        for item in rankings:
            if len(result) >= self.params["holdings_num"]:
                break
            symbol = item["etf"]
            if symbol not in context.universe:
                continue
            if recheck and self._profit_triggered(context, symbol):
                continue
            result.append(symbol)
        if not result:
            defensive = self.params["defensive_symbol"]
            if defensive in context.event_prices:
                result = [defensive]
        return result

    def _sell_intents(self, context, rankings: list[dict]) -> list[OrderIntent]:
        targets = set(self._targets(context, rankings, recheck=False))
        relevant = [*context.all_candidate_symbols, self.params["defensive_symbol"]]
        return [
            OrderIntent(
                symbol=symbol, action="SELL", sizing_mode="TARGET", value_percent=0,
                reason=f"七星排名换仓，目标为 {', '.join(targets) or '现金'}",
            )
            for symbol in relevant
            if context.portfolio.quantity(symbol) > 0 and symbol not in targets
        ]

    def _buy_intents(self, context, rankings: list[dict]) -> list[OrderIntent]:
        targets = self._targets(context, rankings, recheck=True)
        relevant = [*context.all_candidate_symbols, self.params["defensive_symbol"]]
        if any(
            context.portfolio.quantity(symbol) > 0 and symbol not in targets
            for symbol in relevant
        ):
            return []
        if not targets:
            return []
        target_percent = 100.0 / len(targets)
        tolerance = self.params["rebalance_tolerance_percent"] / 100
        equity = float(context.portfolio.equity(context.marks))
        intents = []
        for symbol in targets:
            target_value = (
                equity
                * float(context.portfolio.effective_leverage(symbol))
                / len(targets)
            )
            current_value = float(context.portfolio.quantity(symbol)) * context.marks[symbol]
            within_tolerance = (
                current_value != 0
                and abs(current_value - target_value) <= target_value * tolerance
            )
            if within_tolerance:
                continue
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    action="BUY" if current_value < target_value else "SELL",
                    sizing_mode="TARGET", value_percent=target_percent,
                    reason=f"七星目标等权 {target_percent:.4f}%",
                    minimum_trade_value=self.params["minimum_trade_value_usd"],
                )
            )
        return intents


STRATEGY_REGISTRY: dict[str, type[CodeStrategy]] = {
    RapidDropAtrRotationStrategy.key: RapidDropAtrRotationStrategy,
    RapidDropWtmeRotationStrategy.key: RapidDropWtmeRotationStrategy,
    SevenStarEtfRotationStrategy.key: SevenStarEtfRotationStrategy,
}


def list_code_strategies() -> list[dict]:
    return [strategy.catalog_item() for strategy in STRATEGY_REGISTRY.values()]


def get_code_strategy(code_key: str) -> type[CodeStrategy]:
    try:
        return STRATEGY_REGISTRY[code_key]
    except KeyError as exc:
        raise BacktestValidationError(f"未知代码策略：{code_key}。") from exc
