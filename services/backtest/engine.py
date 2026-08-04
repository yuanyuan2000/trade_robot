from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from database import intraday_repository

from services.backtest.code_strategies import get_code_strategy
from services.backtest.data import HistoricalDataSet, load_historical_dataset
from services.backtest.dsl import CompiledExpression, compile_expression
from services.backtest.errors import (
    BacktestCancelled,
    BacktestDataError,
    BacktestOrderError,
    BacktestValidationError,
)
from services.backtest.metrics import calculate_metrics
from services.backtest.portfolio import OrderIntent, Portfolio
from services.backtest.validation import validate_settings, validate_strategy_payload


def _event_sort_key(event: str) -> tuple[int, str]:
    if event == "OPEN":
        return (0, "09:30")
    if event == "CLOSE":
        return (2, "16:00")
    return (1, event)


@dataclass
class BacktestResult:
    metrics: dict
    equity_points: list[dict]
    trades: list[dict]
    logs: list[dict]
    data_manifest: dict
    termination_reason: str | None = None
    liquidation: dict | None = None

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics,
            "equity_points": self.equity_points,
            "trades": self.trades,
            "logs": self.logs,
            "data_manifest": self.data_manifest,
            "termination_reason": self.termination_reason,
            "liquidation": self.liquidation,
        }


class CodeEventContext:
    def __init__(
        self,
        *,
        dataset: HistoricalDataSet,
        portfolio: Portfolio,
        universe: list[str],
        trading_date: str,
        event: str,
        event_prices: dict,
        marks: dict[str, float],
        all_candidate_symbols: list[str],
        log_callback: Callable[..., None],
    ):
        self.dataset = dataset
        self.portfolio = portfolio
        self.universe = universe
        self.trading_date = trading_date
        self.event = event
        self.event_prices = event_prices
        self.marks = marks
        self.all_candidate_symbols = all_candidate_symbols
        self._log_callback = log_callback

    def expression_context(self, symbol: str):
        return self.dataset.expression_context(
            symbol=symbol,
            trading_date=self.trading_date,
            event=self.event,
            price=self.event_prices[symbol].signal_price,
            position=float(self.portfolio.weight(symbol, self.marks)),
        )

    def log_custom(
        self,
        event_type: str,
        message: str,
        *,
        symbol: str | None = None,
        context: dict | None = None,
        level: str = "DEBUG",
    ) -> None:
        """Write a structured code-strategy log that can become XLS columns."""
        self._log_callback(
            level,
            event_type,
            message,
            event_time=f"{self.trading_date} {self.event}",
            symbol=symbol,
            context=context,
        )

    def log_strategy_evaluations(self, evaluations: list[dict]) -> None:
        for item in evaluations:
            formula_mode = item.get("trend_formula_mode")
            formula_label = {
                "consistent_w2": "一致加权 R²",
                "legacy_v1": "历史 v1.0.0 不一致权重",
            }.get(formula_mode, formula_mode)
            formula_text = (
                f"，公式 {formula_label}"
                if formula_label
                else ""
            )
            reasons = item.get("filter_reasons") or []
            filter_text = f"，硬性过滤：{'；'.join(reasons)}" if reasons else "，通过硬性过滤"
            rank_text = (
                f"，合格排名第 {item['rank']}"
                if item.get("rank") is not None
                else ""
            )
            self.log_custom(
                "SEVENSTAR_DAILY_SCORE",
                f"{item['etf']} 趋势评分 {item['score']:.8f}{formula_text}"
                f"{rank_text}{filter_text}。",
                symbol=item["etf"],
                context=item,
            )


class BacktestEngine:
    def __init__(
        self,
        strategy: dict,
        settings: dict,
        *,
        dataset: HistoricalDataSet | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ):
        self.strategy = validate_strategy_payload(strategy)
        self.settings = validate_settings(settings)
        self.definition = self.strategy["definition"]
        self.universe = [item["symbol"] for item in self.definition["symbols"]]
        self.max_weights = {
            item["symbol"]: float(item["max_weight"])
            for item in self.definition["symbols"]
        }
        self.symbol_leverages = {
            item["symbol"]: float(item.get("leverage_multiplier", 1))
            for item in self.definition["symbols"]
        }
        self.progress_callback = progress_callback
        self.cancellation_check = cancellation_check or (lambda: False)
        self.logs: list[dict] = []
        self.trades: list[dict] = []
        self.equity_points: list[dict] = []
        self._compiled_rules: list[tuple[dict, CompiledExpression]] = []
        self._competition_eligibility: CompiledExpression | None = None
        self._competition_score: CompiledExpression | None = None
        self.code_strategy = None
        self.auxiliary_symbols: list[str] = []
        self.early_close_offsets: dict[str, int] = {}
        self.cumulative_volume_events: tuple[str, ...] = ()
        requirements = self._build_requirements()
        self.events = requirements["events"]
        self.minimum_lookback = requirements["minimum_lookback"]
        if self.code_strategy is not None:
            minimum_trade = float(self.code_strategy.params.get("minimum_trade_value_usd", 0))
            holdings = int(self.code_strategy.params.get("holdings_num", 1))
            if minimum_trade >= (
                self.settings["initial_capital"]
                * self.settings["leverage_multiplier"]
                * min(self.symbol_leverages.values(), default=1)
                / holdings
            ):
                raise BacktestValidationError(
                    "最小非清仓交易额必须小于杠杆后可用敞口除以目标持仓数量。"
                )
        self.tradable_symbols = list(
            dict.fromkeys([*self.universe, *self.auxiliary_symbols])
        )
        for symbol in self.auxiliary_symbols:
            self.max_weights[symbol] = 100.0
            self.symbol_leverages.setdefault(symbol, 1.0)
        self.benchmark_weights = self._benchmark_weights()
        additional = [
            symbol
            for symbol in self.benchmark_weights
            if symbol not in self.tradable_symbols
        ]
        self.dataset = dataset or load_historical_dataset(
            universe=self.tradable_symbols,
            additional_symbols=additional,
            start_date=self.settings["start_date"],
            end_date=self.settings["end_date"],
            intraday_events=self.events,
            minimum_lookback=self.minimum_lookback,
            early_close_offsets=self.early_close_offsets,
            cumulative_volume_events=self.cumulative_volume_events,
            optional_symbols=self.auxiliary_symbols,
        )
        self._validate_supplied_dataset(additional)
        portfolio_kwargs = {
            "commission_per_share": self.settings["commission_per_share"],
            "minimum_commission": self.settings["minimum_commission"],
            "slippage_bps": self.settings["slippage_bps"],
            "allow_fractional_shares": self.settings["allow_fractional_shares"],
            "quantity_steps": {
                symbol: details.get("quantity_step") or (
                    0.0001 if symbol == "BTC/USD" else None
                )
                for symbol, details in self.dataset.manifest.get("symbols", {}).items()
                if details.get("quantity_step") or symbol == "BTC/USD"
            },
        }
        self.portfolio = Portfolio(
            self.settings["initial_capital"],
            leverage_multiplier=self.settings["leverage_multiplier"],
            symbol_leverage_multipliers=self.symbol_leverages,
            **portfolio_kwargs,
        )
        self.benchmark = (
            Portfolio(self.settings["initial_capital"], **portfolio_kwargs)
            if self.benchmark_weights
            else None
        )
        self.termination_reason: str | None = None
        self.liquidation: dict | None = None
        self._liquidation_minute_audit: dict[str, dict[int, dict]] = {}
        self.max_observed_gross_leverage = 0.0

    def _build_requirements(self) -> dict:
        events: set[str] = set()
        lookback = 1
        if self.strategy["design_mode"] == "visual":
            for rule in self.definition["rules"]:
                if not rule["enabled"]:
                    continue
                compiled = compile_expression(rule["condition"])
                self._compiled_rules.append((rule, compiled))
                events.add(rule["when"])
                lookback = max(lookback, compiled.max_lookback)
            self._compiled_rules.sort(
                key=lambda item: (int(item[0]["priority"]), item[0]["id"])
            )
            if self.strategy["selection_mode"] == "competition":
                competition = self.definition["competition"]
                self._competition_eligibility = compile_expression(
                    competition["eligibility"]
                )
                self._competition_score = compile_expression(competition["score"])
                events.add(competition["when"])
                lookback = max(
                    lookback,
                    self._competition_eligibility.max_lookback,
                    self._competition_score.max_lookback,
                )
        else:
            strategy_type = get_code_strategy(self.strategy["code_key"])
            if self.strategy.get("code_version") not in {None, strategy_type.version}:
                raise BacktestValidationError(
                    f"策略版本 {self.strategy['code_version']} 与当前代码版本 "
                    f"{strategy_type.version} 不一致，请保存升级后的策略后再运行。"
                )
            params = strategy_type.validate_params(self.definition.get("params", {}))
            strategy_type.validate_definition(self.definition)
            self.code_strategy = strategy_type(params)
            events.update(strategy_type.required_events(params))
            lookback = max(lookback, strategy_type.minimum_lookback(params))
            self.auxiliary_symbols = list(strategy_type.additional_symbols(params))
            self.early_close_offsets = strategy_type.early_close_offsets(params)
            self.cumulative_volume_events = strategy_type.cumulative_volume_events(params)
        return {
            "events": sorted(events, key=_event_sort_key),
            "minimum_lookback": lookback,
        }

    def _validate_supplied_dataset(self, additional: list[str]) -> None:
        missing_symbols = [
            symbol
            for symbol in [*self.universe, *additional]
            if symbol not in self.dataset.daily
        ]
        if missing_symbols:
            raise BacktestDataError(f"数据集缺少标的：{missing_symbols}。")
        if not self.dataset.sessions:
            raise BacktestDataError("数据集没有可执行交易日。")
        # Per-symbol warmup is represented by dataset.availability_start. Late
        # inception symbols become eligible only after their own warmup completes.

    def _benchmark_weights(self) -> dict[str, float]:
        benchmark = self.settings["benchmark"]
        if benchmark == "none":
            return {}
        if benchmark in {"SPY", "GLD"}:
            return {benchmark: 100.0}
        if self.strategy["selection_mode"] == "single":
            return {self.universe[0]: 100.0}
        if self.strategy["selection_mode"] == "distribution":
            return dict(self.max_weights)
        equal = 100.0 / len(self.universe)
        return {symbol: equal for symbol in self.universe}

    def _log(
        self,
        level: str,
        event_type: str,
        message: str,
        *,
        event_time: str | None = None,
        symbol: str | None = None,
        context: dict | None = None,
    ) -> None:
        self.logs.append(
            {
                "sequence": len(self.logs) + 1,
                "event_time": event_time,
                "level": level,
                "event_type": event_type,
                "symbol": symbol,
                "message": message,
                "context": context,
                "created_at": datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat(),
            }
        )

    def run(self) -> BacktestResult:
        self._log(
            "INFO",
            "RUN_START",
            f"开始回测，共 {len(self.dataset.sessions)} 个交易日。",
            context={
                "strategy": self.strategy["name"],
                "settings": self.settings,
                "events": self.events,
            },
        )
        pending_close: list[OrderIntent] = []
        peak = float(self.settings["initial_capital"])

        for day_index, trading_date in enumerate(self.dataset.sessions):
            if self.cancellation_check():
                raise BacktestCancelled("用户取消了回测。")

            self._apply_corporate_actions(trading_date)
            self._update_benchmark(trading_date)

            active_tradable = self.dataset.active_symbols(
                self.tradable_symbols, trading_date
            )
            open_marks = self.dataset.open_prices(trading_date, active_tradable)
            if self._check_and_liquidate(
                marks=open_marks,
                event_time=f"{trading_date} 09:30 America/New_York",
                trading_date=trading_date,
                trigger="OPEN",
            ):
                peak = self._finish_liquidated_day(day_index, trading_date, peak)
                break

            if pending_close:
                open_event = {
                    symbol: self.dataset.event_price(symbol, trading_date, "OPEN")
                    for symbol in self.dataset.active_symbols(
                        self.tradable_symbols, trading_date
                    )
                }
                self._execute_intents(
                    pending_close,
                    trading_date=trading_date,
                    event="OPEN",
                    event_prices=open_event,
                    reason_prefix="前一交易日收盘信号",
                )
                pending_close = []

            if self._check_and_liquidate(
                marks=open_marks,
                event_time=f"{trading_date} 09:30 America/New_York",
                trading_date=trading_date,
                trigger="POST_TRADE",
            ):
                peak = self._finish_liquidated_day(day_index, trading_date, peak)
                break

            open_minute, close_minute = self.dataset.session_minutes(trading_date)
            risk_cursor = open_minute
            risk_marks = dict(open_marks)

            for event in self.events:
                event_minute = (
                    open_minute
                    if event == "OPEN"
                    else close_minute
                    if event == "CLOSE"
                    else self._event_fill_minute(trading_date, event)
                )
                if self._scan_liquidation_interval(
                    trading_date=trading_date,
                    start_minute=risk_cursor,
                    end_minute=event_minute,
                    initial_marks=risk_marks,
                ):
                    break
                active_tradable = self.dataset.active_symbols(
                    self.tradable_symbols, trading_date
                )
                event_prices = {
                    symbol: self.dataset.event_price(symbol, trading_date, event)
                    for symbol in active_tradable
                }
                intents = self._strategy_intents(
                    trading_date=trading_date,
                    event=event,
                    event_prices=event_prices,
                )
                if event == "CLOSE":
                    if self.dataset.next_session(trading_date) is None:
                        if intents:
                            self._log(
                                "WARN",
                                "UNFILLED_CLOSE_SIGNAL",
                                "回测结束日的收盘信号没有下一交易日可供成交，已丢弃。",
                                event_time=f"{trading_date} CLOSE",
                                context={"orders": len(intents)},
                            )
                    else:
                        pending_close = intents
                else:
                    self._execute_intents(
                        intents,
                        trading_date=trading_date,
                        event=event,
                        event_prices=event_prices,
                    )

                event_marks = {
                    symbol: float(
                        value.fill_price
                        if value.fill_price is not None
                        else value.signal_price
                    )
                    for symbol, value in event_prices.items()
                }
                risk_marks.update(event_marks)
                risk_cursor = max(risk_cursor, event_minute)
                if self._check_and_liquidate(
                    marks=event_marks,
                    event_time=(
                        next(
                            (
                                value.fill_time
                                for value in event_prices.values()
                                if value.fill_time
                            ),
                            f"{trading_date} {event}",
                        )
                    ),
                    trading_date=trading_date,
                    trigger="POST_TRADE",
                ):
                    break

            if not self.termination_reason:
                self._scan_liquidation_interval(
                    trading_date=trading_date,
                    start_minute=risk_cursor,
                    end_minute=close_minute,
                    initial_marks=risk_marks,
                )

            if self.termination_reason:
                peak = self._finish_liquidated_day(day_index, trading_date, peak)
                break

            held_symbols = [
                symbol for symbol in self.tradable_symbols
                if self.portfolio.quantity(symbol) > 0
            ]
            close_marks = self.dataset.close_prices(trading_date, held_symbols)
            equity = float(self.portfolio.equity(close_marks))
            positions_value = float(self.portfolio.market_value(close_marks))
            peak = max(peak, equity)
            drawdown = equity / peak - 1 if peak > 0 else 0
            benchmark_equity = self._benchmark_equity(trading_date)
            point = {
                "sequence": len(self.equity_points) + 1,
                "trading_date": trading_date,
                "cash": float(self.portfolio.cash),
                "receivables": float(self.portfolio.receivable_value),
                "positions_value": positions_value,
                "equity": equity,
                "borrowed_cash": float(self.portfolio.borrowed_cash),
                "gross_leverage": float(self.portfolio.gross_leverage(close_marks)),
                "return_rate": equity / self.settings["initial_capital"] - 1,
                "drawdown_rate": drawdown,
                "benchmark_equity": benchmark_equity,
                "benchmark_return_rate": (
                    benchmark_equity / self.settings["initial_capital"] - 1
                    if benchmark_equity is not None
                    else None
                ),
                "positions": self.portfolio.snapshot(close_marks),
            }
            self.equity_points.append(point)
            self._log(
                "DEBUG",
                "DAILY_SNAPSHOT",
                (
                    f"{trading_date} 收盘：现金 {point['cash']:.2f}，"
                    f"应收股息 {point['receivables']:.2f}，"
                    f"持仓市值 {positions_value:.2f}，总权益 {equity:.2f}，"
                    f"当期回撤 {drawdown:.2%}。"
                ),
                event_time=f"{trading_date} CLOSE",
                context={
                    "positions": point["positions"],
                    "equity": equity,
                    "return_rate": point["return_rate"],
                    "drawdown_rate": drawdown,
                },
            )
            if self.progress_callback:
                self.progress_callback(
                    {
                        "progress": (day_index + 1) / len(self.dataset.sessions),
                        "current_time": trading_date,
                        "equity_point": point,
                        "trade_count": len(self.trades),
                        "log_count": len(self.logs),
                    }
                )

        metrics = calculate_metrics(
            self.equity_points,
            self.trades,
            initial_capital=self.settings["initial_capital"],
            risk_free_rate=self.settings["risk_free_rate"],
            total_commission=float(self.portfolio.total_commission),
            total_slippage=float(self.portfolio.total_slippage),
            termination_reason=self.termination_reason,
            liquidation=self.liquidation,
            leverage_multiplier=self.settings["leverage_multiplier"],
            max_observed_gross_leverage=self.max_observed_gross_leverage,
        )
        self._finalize_liquidation_manifest()
        if self.termination_reason == "LIQUIDATED":
            self._log(
                "WARN",
                "RUN_LIQUIDATED",
                (
                    f"回测因爆仓提前结束，期末权益 {metrics['ending_equity']:.2f}，"
                    f"总收益率 {metrics['total_return']:.2%}。"
                ),
                event_time=(
                    self.liquidation.get("liquidation_time")
                    if self.liquidation
                    else None
                ),
                context=metrics,
            )
        else:
            self._log(
                "INFO",
                "RUN_COMPLETE",
                (
                    f"回测完成，期末权益 {metrics['ending_equity']:.2f}，"
                    f"总收益率 {metrics['total_return']:.2%}。"
                ),
                context=metrics,
            )
        return BacktestResult(
            metrics=metrics,
            equity_points=self.equity_points,
            trades=self.trades,
            logs=self.logs,
            data_manifest=self.dataset.manifest,
            termination_reason=self.termination_reason,
            liquidation=self.liquidation,
        )

    def _event_fill_minute(self, trading_date: str, event: str) -> int:
        resolutions = self.dataset.intraday_event_minutes
        for symbol in self.dataset.active_symbols(self.tradable_symbols, trading_date):
            value = resolutions.get(symbol, {}).get(f"{trading_date}|{event}")
            if value and value.get("fill_minute") is not None:
                return int(value["fill_minute"])
        from services.backtest.data import _epoch_minute

        return _epoch_minute(trading_date, event)

    def _scan_liquidation_interval(
        self,
        *,
        trading_date: str,
        start_minute: int,
        end_minute: int,
        initial_marks: dict[str, float],
    ) -> bool:
        if (
            self.portfolio.max_leverage_multiplier <= 1
            or self.termination_reason
            or end_minute <= start_minute
        ):
            return False
        held = [
            symbol for symbol in self.tradable_symbols
            if self.portfolio.quantity(symbol) > 0
        ]
        if not held:
            return False
        low_marks = {
            symbol: float(self.dataset.day_bar(symbol, trading_date)["low"])
            for symbol in held
        }
        if self.portfolio.equity(low_marks) > 0:
            return False

        bars_by_symbol: dict[str, dict[int, dict]] = {}
        for symbol in held:
            bars = {}
            for row in intraday_repository.get_minute_bars(
                symbol,
                start_minute=start_minute,
                end_minute=end_minute - 1,
                limit=20_000,
            ):
                bars[int(row["minute_utc"])] = row
            bars.update(
                {
                    int(minute): row
                    for minute, row in self.dataset.minute.get(symbol, {}).items()
                    if start_minute <= int(minute) < end_minute
                }
            )
            if not bars:
                raise BacktestDataError(
                    f"{symbol} 在 {trading_date} 缺少爆仓检查所需的分钟行情。",
                    detail={
                        "symbol": symbol,
                        "type": "liquidation_minute_missing",
                        "start_minute": start_minute,
                        "end_minute": end_minute,
                    },
                )
            bars_by_symbol[symbol] = bars
            self._liquidation_minute_audit.setdefault(symbol, {}).update(bars)

        marks = {
            symbol: float(
                initial_marks.get(
                    symbol,
                    self.dataset.day_bar(symbol, trading_date)["open"],
                )
            )
            for symbol in held
        }
        minute_values = sorted(
            {
                minute
                for bars in bars_by_symbol.values()
                for minute in bars
            }
        )
        for minute in minute_values:
            for symbol, bars in bars_by_symbol.items():
                if minute in bars:
                    marks[symbol] = float(bars[minute]["low"])
            if self._check_and_liquidate(
                marks=marks,
                event_time=self._minute_event_time(minute),
                trading_date=trading_date,
                trigger="MINUTE_LOW",
            ):
                return True
            for symbol, bars in bars_by_symbol.items():
                if minute in bars:
                    marks[symbol] = float(bars[minute]["close"])
        return False

    def _finalize_liquidation_manifest(self) -> None:
        from services.backtest.data import _sha256

        records = [
            {
                "symbol": symbol,
                "minute_utc": minute,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
            for symbol, bars in sorted(self._liquidation_minute_audit.items())
            for minute, row in sorted(bars.items())
        ]
        self.dataset.manifest["liquidation_monitor"] = {
            "granularity": "minute_low",
            "points_checked": len(records),
            "sha256": _sha256(records),
        }

    @staticmethod
    def _minute_event_time(minute: int) -> str:
        from services.backtest.data import _minute_label

        return _minute_label(minute)

    def _check_and_liquidate(
        self,
        *,
        marks: dict[str, float],
        event_time: str,
        trading_date: str,
        trigger: str,
    ) -> bool:
        if self.portfolio.max_leverage_multiplier <= 1 or self.termination_reason:
            return bool(self.termination_reason)
        held = [
            symbol for symbol in self.tradable_symbols
            if self.portfolio.quantity(symbol) > 0
        ]
        if not held:
            return False
        held_marks = {symbol: marks[symbol] for symbol in held if symbol in marks}
        if len(held_marks) != len(held):
            missing = sorted(set(held) - set(held_marks))
            raise BacktestDataError(f"爆仓检查缺少持仓盯市价格：{missing}。")
        equity_before = float(self.portfolio.equity(held_marks))
        gross_before = float(self.portfolio.market_value(held_marks))
        if equity_before > 0:
            self.max_observed_gross_leverage = max(
                self.max_observed_gross_leverage,
                gross_before / equity_before,
            )
            return False
        forced_trades = self.portfolio.liquidate_all(
            marks=held_marks,
            event_time=event_time,
        )
        for trade in forced_trades:
            self.trades.append(trade)
            self._log_trade(trade)
        equity_after = float(self.portfolio.equity({}))
        self.termination_reason = "LIQUIDATED"
        self.liquidation = {
            "liquidated": True,
            "liquidation_time": event_time,
            "trading_date": trading_date,
            "trigger": trigger,
            "equity_before_liquidation": equity_before,
            "equity_after_liquidation": equity_after,
            "gross_exposure_before_liquidation": gross_before,
            "forced_trade_count": len(forced_trades),
            "leverage_multiplier": self.settings["leverage_multiplier"],
        }
        self._log(
            "ERROR",
            "LIQUIDATION",
            f"账户权益 {equity_before:.2f} 不大于 0，已强制平仓并提前结束回测。",
            event_time=event_time,
            context=self.liquidation,
        )
        return True

    def _finish_liquidated_day(
        self,
        day_index: int,
        trading_date: str,
        peak: float,
    ) -> float:
        equity = float(self.portfolio.equity({}))
        peak = max(peak, equity)
        drawdown = equity / peak - 1 if peak > 0 else 0
        point = {
            "sequence": len(self.equity_points) + 1,
            "trading_date": trading_date,
            "cash": float(self.portfolio.cash),
            "receivables": float(self.portfolio.receivable_value),
            "positions_value": 0.0,
            "equity": equity,
            "borrowed_cash": float(self.portfolio.borrowed_cash),
            "gross_leverage": 0.0,
            "return_rate": equity / self.settings["initial_capital"] - 1,
            "drawdown_rate": drawdown,
            "benchmark_equity": None,
            "benchmark_return_rate": None,
            "positions": {},
        }
        self.equity_points.append(point)
        if self.progress_callback:
            self.progress_callback(
                {
                    "progress": (day_index + 1) / len(self.dataset.sessions),
                    "current_time": self.liquidation["liquidation_time"],
                    "equity_point": point,
                    "trade_count": len(self.trades),
                    "log_count": len(self.logs),
                }
            )
        return peak

    def _marks_for_event(self, event_prices: dict) -> dict[str, float]:
        return {
            symbol: float(value.signal_price)
            for symbol, value in event_prices.items()
        }

    def _strategy_intents(
        self,
        *,
        trading_date: str,
        event: str,
        event_prices: dict,
    ) -> list[OrderIntent]:
        marks = self._marks_for_event(event_prices)
        if self.code_strategy is not None:
            context = CodeEventContext(
                dataset=self.dataset,
                portfolio=self.portfolio,
                universe=self.dataset.active_symbols(self.universe, trading_date),
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
                marks=marks,
                all_candidate_symbols=self.universe,
                log_callback=self._log,
            )
            return list(self.code_strategy.on_event(context))
        if self.strategy["selection_mode"] == "competition":
            return self._competition_intents(
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
                marks=marks,
            )
        intents = []
        for symbol in event_prices:
            if symbol not in self.universe:
                continue
            intent, _ = self._first_matching_rule(
                symbol=symbol,
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
                marks=marks,
            )
            if intent:
                intents.append(intent)
        return intents

    def _first_matching_rule(
        self,
        *,
        symbol: str,
        trading_date: str,
        event: str,
        event_prices: dict,
        marks: dict[str, float],
    ) -> tuple[OrderIntent | None, bool]:
        for rule, compiled in self._compiled_rules:
            if not rule["enabled"] or rule["when"] != event:
                continue
            context = self.dataset.expression_context(
                symbol=symbol,
                trading_date=trading_date,
                event=event,
                price=event_prices[symbol].signal_price,
                position=float(self.portfolio.weight(symbol, marks)),
            )
            matched = bool(compiled.evaluate(context))
            self._log(
                "DEBUG",
                "RULE_EVALUATION",
                f"{symbol} 规则“{rule['name']}”判断为 {matched}。",
                event_time=f"{trading_date} {event}",
                symbol=symbol,
                context={
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "condition": rule["condition"],
                    "price": context.price,
                    "position": context.position,
                    "inputs": compiled.resolve_inputs(context),
                    "matched": matched,
                },
            )
            if not matched:
                continue
            if rule["action"] == "HOLD":
                return None, True
            return (
                OrderIntent(
                    symbol=symbol,
                    action=rule["action"],
                    sizing_mode=rule["sizing_mode"],
                    value_percent=rule["value"],
                    reason=f"规则命中：{rule['name']}",
                ),
                True,
            )
        return None, False

    def _competition_intents(
        self,
        *,
        trading_date: str,
        event: str,
        event_prices: dict,
        marks: dict[str, float],
    ) -> list[OrderIntent]:
        risk_intents: list[OrderIntent] = []
        blocked: set[str] = set()
        held_rule_matched = False
        active_candidates = [symbol for symbol in self.universe if symbol in event_prices]
        for symbol in active_candidates:
            intent, matched = self._first_matching_rule(
                symbol=symbol,
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
                marks=marks,
            )
            if matched:
                blocked.add(symbol)
                if self.portfolio.quantity(symbol) > 0:
                    held_rule_matched = True
            if intent:
                risk_intents.append(intent)
        competition = self.definition["competition"]
        if event != competition["when"]:
            return risk_intents
        # A matched ordinary rule on the current holding has precedence.
        # This preserves HOLD and partial SELL semantics and prevents a second
        # competition-generated order from silently overriding its target.
        if held_rule_matched:
            return risk_intents

        scores: list[tuple[float, str]] = []
        for symbol in active_candidates:
            if symbol in blocked:
                continue
            context = self.dataset.expression_context(
                symbol=symbol,
                trading_date=trading_date,
                event=event,
                price=event_prices[symbol].signal_price,
                position=float(self.portfolio.weight(symbol, marks)),
            )
            if bool(self._competition_eligibility.evaluate(context)):
                score = float(self._competition_score.evaluate(context))
                scores.append((score, symbol))
                self._log(
                    "DEBUG",
                    "COMPETITION_SCORE",
                    f"{symbol} 竞争评分 {score:.8f}。",
                    event_time=f"{trading_date} {event}",
                    symbol=symbol,
                    context={"score": score},
                )
        winner = sorted(scores, key=lambda item: (-item[0], item[1]))[0][1] if scores else None
        selection_intents = [
            OrderIntent(
                symbol=symbol,
                action="SELL",
                sizing_mode="TARGET",
                value_percent=0,
                reason=f"竞争模式换仓，胜出标的为 {winner or '无'}",
            )
            for symbol in self.universe
            if symbol != winner and self.portfolio.quantity(symbol) > 0
        ]
        if winner:
            selection_intents.append(
                OrderIntent(
                    symbol=winner,
                    action="BUY",
                    sizing_mode="TARGET",
                    value_percent=competition["target_weight"],
                    reason=f"竞争评分最高：{winner}",
                )
            )
        elif not competition["cash_when_none"]:
            selection_intents = []
        return [*risk_intents, *selection_intents]

    def _execute_intents(
        self,
        intents: list[OrderIntent],
        *,
        trading_date: str,
        event: str,
        event_prices: dict,
        reason_prefix: str | None = None,
    ) -> None:
        ordered = sorted(intents, key=lambda order: 0 if order.action == "SELL" else 1)
        marks = {
            symbol: float(
                value.fill_price
                if value.fill_price is not None
                else value.signal_price
            )
            for symbol, value in event_prices.items()
        }
        for intent in ordered:
            value = event_prices[intent.symbol]
            reference = value.fill_price
            if reference is None:
                raise BacktestOrderError("收盘信号不能在同一收盘价成交。")
            effective_intent = (
                OrderIntent(
                    symbol=intent.symbol,
                    action=intent.action,
                    sizing_mode=intent.sizing_mode,
                    value_percent=intent.value_percent,
                    reason=f"{reason_prefix}；{intent.reason}",
                    minimum_trade_value=intent.minimum_trade_value,
                )
                if reason_prefix
                else intent
            )
            try:
                trade = self.portfolio.execute(
                    effective_intent,
                    reference_price=reference,
                    marks=marks,
                    max_weight_percent=self.max_weights[intent.symbol],
                    event_time=value.fill_time or f"{trading_date} {event}",
                )
            except BacktestOrderError as exc:
                self._log(
                    "ERROR",
                    "ORDER_REJECTED",
                    exc.message,
                    event_time=f"{trading_date} {event}",
                    symbol=intent.symbol,
                    context={"order": effective_intent.__dict__},
                )
                raise
            if not trade:
                continue
            self.trades.append(trade)
            self._log_trade(trade)

    def _log_trade(self, trade: dict) -> None:
        realized = (
            f"，已实现PnL {trade['realized_pnl']:.2f}"
            if trade["realized_pnl"] is not None
            else ""
        )
        self._log(
            "INFO",
            "TRADE",
            (
                f"{trade['side']} {trade['symbol']} {trade['quantity']:g} 股 "
                f"@ {trade['fill_price']:.6f}，手续费 "
                f"{trade['commission']:.4f}，滑点成本 "
                f"{trade['slippage_amount']:.4f}{realized}。"
            ),
            event_time=trade["event_time"],
            symbol=trade["symbol"],
            context=trade,
        )

    def _update_benchmark(self, trading_date: str) -> None:
        if self.benchmark is None:
            return
        active = self.dataset.active_symbols(self.benchmark_weights, trading_date)
        marks = self.dataset.open_prices(trading_date, active)
        for symbol in active:
            weight = self.benchmark_weights[symbol]
            if self.benchmark.quantity(symbol) > 0:
                continue
            self.benchmark.execute(
                OrderIntent(
                    symbol=symbol,
                    action="BUY",
                    sizing_mode="TARGET",
                    value_percent=weight,
                    reason="比较基准期初买入并持有",
                ),
                reference_price=marks[symbol],
                marks=marks,
                max_weight_percent=weight,
                event_time=f"{trading_date} OPEN",
            )

    def _apply_corporate_actions(self, trading_date: str) -> None:
        for payment in self.portfolio.settle_receivables(trading_date):
            self._log(
                "INFO",
                "DIVIDEND_PAYMENT",
                (
                    f"{payment['symbol']} 现金分红到账 "
                    f"{payment['amount']:.4f}。"
                ),
                event_time=f"{trading_date} OPEN",
                symbol=payment["symbol"],
                context=payment,
            )
        actions = [
            action
            for action in self.dataset.corporate_actions_on(trading_date)
            if action["symbol"] in self.tradable_symbols
        ]
        for event in self.portfolio.apply_corporate_actions(
            actions,
            trading_date=trading_date,
        ):
            if event["type"] == "split":
                message = (
                    f"{event['symbol']} 拆股比例 {event['ratio']:.8g}，"
                    f"持仓 {event['quantity_before']:g} 调整为 "
                    f"{event['quantity_after']:g} 股。"
                )
                event_type = "STOCK_SPLIT"
            else:
                message = (
                    f"{event['symbol']} 每股分红 {event['rate']:.6f}，"
                    f"确认应收 {event['amount']:.4f}，"
                    f"支付日 {event['payable_date']}。"
                )
                event_type = "DIVIDEND_ACCRUAL"
            self._log(
                "INFO",
                event_type,
                message,
                event_time=f"{trading_date} OPEN",
                symbol=event["symbol"],
                context=event,
            )
        if self.benchmark is not None:
            self.benchmark.settle_receivables(trading_date)
            benchmark_actions = [
                action
                for action in self.dataset.corporate_actions_on(trading_date)
                if action["symbol"] in self.benchmark_weights
            ]
            self.benchmark.apply_corporate_actions(
                benchmark_actions,
                trading_date=trading_date,
            )

    def _benchmark_equity(self, trading_date: str) -> float | None:
        if self.benchmark is None:
            return None
        held = [
            symbol for symbol in self.benchmark_weights
            if self.benchmark.quantity(symbol) > 0
        ]
        marks = self.dataset.close_prices(trading_date, held)
        return float(self.benchmark.equity(marks))
