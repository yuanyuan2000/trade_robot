from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.backtest.code_strategies import RapidDropWtmeRotationStrategy
from services.backtest.data import HistoricalDataSet, _epoch_minute
from services.backtest.engine import BacktestEngine
from services.backtest.portfolio import OrderIntent, Portfolio


def business_dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def daily_rows(dates: list[str], closes: list[float]) -> list[dict]:
    rows = []
    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        previous = closes[index - 1] if index else close
        rows.append({
            "date": trading_date,
            "open": previous,
            "high": max(previous, close),
            "low": min(previous, close),
            "close": close,
            "volume": 1000,
        })
    return rows


class WtmeStrategyTests(unittest.TestCase):
    def _selection_context(
        self,
        scores: dict[str, float],
        *,
        portfolio: Portfolio | None = None,
        prices: dict[str, float] | None = None,
    ) -> tuple[SimpleNamespace, list[dict]]:
        symbols = list(scores)
        prices = prices or {
            symbol: 101.0 + index for index, symbol in enumerate(symbols)
        }
        price_scores = {prices[symbol]: score for symbol, score in scores.items()}

        class Dataset:
            @staticmethod
            def daily_before(symbol, trading_date, limit):
                return [
                    {
                        "date": f"2024-01-{index + 1:02d}",
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                    }
                    for index in range(limit)
                ]

        logs: list[dict] = []

        def log_custom(event_type, message, **kwargs):
            logs.append({
                "event_type": event_type,
                "message": message,
                "symbol": kwargs.get("symbol"),
                "context": kwargs.get("context") or {},
            })

        context = SimpleNamespace(
            dataset=Dataset(),
            portfolio=portfolio or Portfolio(10_000, allow_fractional_shares=True),
            universe=symbols,
            trading_date="2024-01-31",
            event="10:00",
            event_prices={
                symbol: SimpleNamespace(signal_price=price)
                for symbol, price in prices.items()
            },
            marks=prices,
            log_custom=log_custom,
            price_scores=price_scores,
        )
        return context, logs

    @staticmethod
    def _mock_wtme(rows, period, half_life, epsilon):
        score = WtmeStrategyTests._active_price_scores[float(rows[-1]["close"])]
        return {
            "value": score,
            "weighted_return": score / 100,
            "weighted_true_range": 1.0,
        }

    _active_price_scores: dict[float, float] = {}

    def _select_with_scores(
        self,
        params: dict,
        scores: dict[str, float],
        *,
        portfolio: Portfolio | None = None,
        prices: dict[str, float] | None = None,
    ):
        strategy = RapidDropWtmeRotationStrategy(params)
        context, logs = self._selection_context(
            scores, portfolio=portfolio, prices=prices
        )
        type(self)._active_price_scores = context.price_scores
        with patch(
            "services.backtest.code_strategies.calculate_wtme_components",
            side_effect=self._mock_wtme,
        ):
            intents = strategy._select(context)
        return strategy, context, logs, intents

    def test_strategy_parameters_cover_wtme_filters_and_times(self) -> None:
        params = RapidDropWtmeRotationStrategy.validate_params({})

        self.assertEqual(params["allocation_mode"], "equal")
        self.assertEqual(params["buy_top_n"], 1)
        self.assertEqual(params["buy_score_threshold"], 9999)
        self.assertEqual(params["max_simultaneous_holdings"], 1)
        for retired in RapidDropWtmeRotationStrategy.retired_parameters:
            self.assertNotIn(retired, params)
        self.assertEqual(params["wtme_period"], 40)
        self.assertEqual(params["wtme_half_life"], 15.0)
        self.assertEqual(params["wtme_epsilon"], 1e-8)
        self.assertFalse(params["enable_upside_sell_protection"])
        self.assertIn("enable_percent_drop_filter", params)
        self.assertNotIn("enable_atr_drop_filter", params)
        self.assertNotIn("drop_threshold_atr", params)
        self.assertNotIn("atr_period", params)
        self.assertNotIn("atr_weighting", params)
        self.assertEqual(params["risk_check_time"], "09:40")
        self.assertEqual(params["selection_time"], "10:00")
        self.assertEqual(
            RapidDropWtmeRotationStrategy.required_events({}),
            ("09:40", "10:00"),
        )

    @staticmethod
    def _portfolio_holding(symbol: str) -> Portfolio:
        portfolio = Portfolio(10_000, allow_fractional_shares=True)
        portfolio.execute(
            OrderIntent(symbol, "BUY", "TARGET", 100, "测试期初持仓"),
            reference_price=100,
            marks={symbol: 100},
            max_weight_percent=100,
            event_time="2024-01-30 10:00",
        )
        return portfolio

    def test_upside_protection_suppresses_rotation_sell_for_rising_holding(self) -> None:
        portfolio = self._portfolio_holding("AAA")
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 1,
                "enable_upside_sell_protection": True,
            },
            {"AAA": 10, "BBB": 20},
            portfolio=portfolio,
            prices={"AAA": 105, "BBB": 101},
        )

        self.assertNotIn(
            ("SELL", "AAA"),
            [(intent.action, intent.symbol) for intent in intents],
        )
        self.assertEqual(intents, [])
        protection = next(
            item for item in logs
            if item["event_type"] == "RAPID_DROP_WTME_UPSIDE_SELL_PROTECTION"
        )
        self.assertEqual(protection["symbol"], "AAA")
        self.assertAlmostEqual(protection["context"]["change"], 0.05)
        score_log = next(
            item for item in logs
            if item["event_type"] == "RAPID_DROP_WTME_DAILY_SCORE"
            and item["symbol"] == "AAA"
        )
        self.assertTrue(score_log["context"]["upside_sell_protection_applied"])
        holdings_block = next(
            item for item in logs
            if item["event_type"] == "RAPID_DROP_WTME_MAX_HOLDINGS_BLOCK"
        )
        self.assertEqual(holdings_block["symbol"], "BBB")
        self.assertEqual(
            holdings_block["context"]["upside_protected_symbols"], ["AAA"]
        )

    def test_upside_protection_does_not_suppress_sell_without_strict_rise(self) -> None:
        portfolio = self._portfolio_holding("AAA")
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 1,
                "enable_upside_sell_protection": True,
            },
            {"AAA": 10, "BBB": 20},
            portfolio=portfolio,
            prices={"AAA": 100, "BBB": 101},
        )

        self.assertIn(
            ("SELL", "AAA"),
            [(intent.action, intent.symbol) for intent in intents],
        )
        self.assertFalse(any(
            item["event_type"] == "RAPID_DROP_WTME_UPSIDE_SELL_PROTECTION"
            for item in logs
        ))

    def test_upside_protection_also_suppresses_partial_rotation_reduction(self) -> None:
        portfolio = self._portfolio_holding("AAA")
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 2,
                "allocation_mode": "linear_rank",
                "enable_upside_sell_protection": True,
                "max_simultaneous_holdings": 2,
            },
            {"AAA": 10, "BBB": 20},
            portfolio=portfolio,
            prices={"AAA": 105, "BBB": 101},
        )

        self.assertNotIn(
            ("SELL", "AAA"),
            [(intent.action, intent.symbol) for intent in intents],
        )
        protection = next(
            item for item in logs
            if item["event_type"] == "RAPID_DROP_WTME_UPSIDE_SELL_PROTECTION"
        )
        self.assertEqual(protection["context"]["planned_order"]["value_percent"], 100 / 3)

    def test_upside_protection_does_not_block_rapid_drop_risk_sell(self) -> None:
        strategy = RapidDropWtmeRotationStrategy({
            "enable_upside_sell_protection": True,
            "drop_threshold_percent": 5,
        })
        context, _logs = self._selection_context(
            {"AAA": 1},
            portfolio=self._portfolio_holding("AAA"),
            prices={"AAA": 90},
        )
        context.event = "09:40"

        intents = strategy._risk_check(context)

        self.assertEqual(
            [(intent.action, intent.symbol) for intent in intents],
            [("SELL", "AAA")],
        )

    def test_unleveraged_full_fractional_holding_has_no_capacity_for_second_buy(self) -> None:
        portfolio = self._portfolio_holding("AAA")

        trade = portfolio.execute(
            OrderIntent("BBB", "BUY", "TARGET", 100, "测试第二持仓"),
            reference_price=100,
            marks={"AAA": 105, "BBB": 100},
            max_weight_percent=100,
            event_time="2024-01-31 10:00",
        )

        self.assertIsNone(trade)
        self.assertEqual(float(portfolio.cash), 0.0)
        self.assertEqual(float(portfolio.quantity("BBB")), 0.0)

    def test_leveraged_gain_creates_financing_headroom_without_positive_cash(self) -> None:
        portfolio = Portfolio(
            10_000,
            allow_fractional_shares=True,
            symbol_leverage_multipliers={"AAA": 3, "BBB": 3},
        )
        portfolio.execute(
            OrderIntent("AAA", "BUY", "TARGET", 100, "测试杠杆期初持仓"),
            reference_price=100,
            marks={"AAA": 100, "BBB": 100},
            max_weight_percent=100,
            event_time="2024-01-30 10:00",
        )
        cash_before = float(portfolio.cash)

        trade = portfolio.execute(
            OrderIntent("BBB", "BUY", "TARGET", 100, "测试融资空间"),
            reference_price=100,
            marks={"AAA": 105, "BBB": 100},
            max_weight_percent=100,
            event_time="2024-01-31 10:00",
        )

        self.assertLess(cash_before, 0)
        self.assertIsNotNone(trade)
        self.assertLess(float(portfolio.cash), cash_before)
        self.assertGreater(float(portfolio.quantity("BBB")), 0)

    def test_buy_conditions_use_or_and_equal_allocation_uses_actual_count(self) -> None:
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 1,
                "buy_score_threshold": 10,
                "max_simultaneous_holdings": 2,
            },
            {"AAA": 30, "BBB": 20, "CCC": 10},
        )

        buys = [intent for intent in intents if intent.action == "BUY"]
        self.assertEqual([intent.symbol for intent in buys], ["AAA", "BBB"])
        self.assertEqual([intent.value_percent for intent in buys], [50.0, 50.0])
        by_symbol = {item["symbol"]: item["context"] for item in logs}
        self.assertTrue(by_symbol["AAA"]["passes_rank_condition"])
        self.assertTrue(by_symbol["BBB"]["passes_score_condition"])
        self.assertFalse(by_symbol["CCC"]["selected_for_target"])
        self.assertTrue(by_symbol["CCC"]["eligible"])
        self.assertEqual(by_symbol["CCC"]["filter_codes"], [])
        self.assertFalse(by_symbol["CCC"]["buy_condition_passed"])
        self.assertIn("buy_conditions", by_symbol["CCC"]["buy_condition_codes"])
        self.assertIn("进入候选名单", next(
            item["message"] for item in logs if item["symbol"] == "CCC"
        ))
        self.assertIn("未进入买入名单", next(
            item["message"] for item in logs if item["symbol"] == "CCC"
        ))

    def test_max_simultaneous_holdings_caps_symbols_passing_buy_conditions(self) -> None:
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 1,
                "buy_score_threshold": 10,
                "max_simultaneous_holdings": 1,
            },
            {"AAA": 30, "BBB": 20, "CCC": 10},
        )

        self.assertEqual(
            [(intent.action, intent.symbol) for intent in intents],
            [("BUY", "AAA")],
        )
        by_symbol = {item["symbol"]: item for item in logs}
        self.assertTrue(
            by_symbol["BBB"]["context"]["excluded_by_max_simultaneous_holdings"]
        )
        self.assertIn("最多同时持仓 1 只", by_symbol["BBB"]["message"])

    def test_linear_rank_allocation_gives_first_place_the_largest_weight(self) -> None:
        _strategy, _context, logs, intents = self._select_with_scores(
            {
                "buy_top_n": 3,
                "allocation_mode": "linear_rank",
                "max_simultaneous_holdings": 3,
            },
            {"AAA": 60, "BBB": 30, "CCC": -10},
        )

        buys = {intent.symbol: intent.value_percent for intent in intents}
        self.assertAlmostEqual(buys["AAA"], 50)
        self.assertAlmostEqual(buys["BBB"], 100 / 3)
        self.assertAlmostEqual(buys["CCC"], 100 / 6)
        by_symbol = {item["symbol"]: item["context"] for item in logs}
        self.assertTrue(by_symbol["CCC"]["selected_for_target"])
        self.assertAlmostEqual(by_symbol["CCC"]["target_weight"], 100 / 6)
        self.assertEqual(by_symbol["CCC"]["filter_codes"], [])

    def test_legacy_score_weighted_value_maps_to_linear_rank(self) -> None:
        self.assertEqual(
            RapidDropWtmeRotationStrategy.validate_params(
                {"allocation_mode": "score_weighted"}
            )["allocation_mode"],
            "linear_rank",
        )

    def test_leveraged_equal_allocation_gives_each_target_full_gross_exposure(self) -> None:
        _strategy, context, _logs, intents = self._select_with_scores(
            {
                "buy_top_n": 3,
                "allocation_mode": "leveraged_equal",
                "max_simultaneous_holdings": 3,
            },
            {"AAA": 30, "BBB": 20, "CCC": 10},
        )

        self.assertTrue(all(intent.value_percent == 100 / 3 for intent in intents))
        self.assertTrue(
            all(float(context.portfolio.effective_leverage(symbol)) == 3 for symbol in context.universe)
        )
        for intent in intents:
            context.portfolio.execute(
                intent,
                reference_price=context.marks[intent.symbol],
                marks=context.marks,
                max_weight_percent=100,
                event_time="2024-01-31 10:00",
            )
        self.assertAlmostEqual(float(context.portfolio.gross_leverage(context.marks)), 3.0, places=5)
        snapshot = context.portfolio.snapshot(context.marks)
        for symbol in context.universe:
            self.assertAlmostEqual(snapshot[symbol]["weight"], 1.0, places=5)
            self.assertAlmostEqual(snapshot[symbol]["strategy_weight"], 1 / 3, places=5)

    def test_leveraged_linear_rank_combines_rank_weights_and_target_count_leverage(self) -> None:
        _strategy, context, _logs, intents = self._select_with_scores(
            {
                "buy_top_n": 3,
                "allocation_mode": "leveraged_linear_rank",
                "max_simultaneous_holdings": 3,
            },
            {"AAA": 30, "BBB": 20, "CCC": 10},
        )

        target_weights = {intent.symbol: intent.value_percent for intent in intents}
        self.assertAlmostEqual(target_weights["AAA"], 50)
        self.assertAlmostEqual(target_weights["BBB"], 100 / 3)
        self.assertAlmostEqual(target_weights["CCC"], 100 / 6)
        self.assertTrue(
            all(
                float(context.portfolio.effective_leverage(symbol)) == 3
                for symbol in context.universe
            )
        )
        for intent in intents:
            context.portfolio.execute(
                intent,
                reference_price=context.marks[intent.symbol],
                marks=context.marks,
                max_weight_percent=100,
                event_time="2024-01-31 10:00",
            )
        snapshot = context.portfolio.snapshot(context.marks)
        self.assertAlmostEqual(float(context.portfolio.gross_leverage(context.marks)), 3.0, places=5)
        for symbol, gross_weight, strategy_weight in (
            ("AAA", 1.5, 0.5),
            ("BBB", 1.0, 1 / 3),
            ("CCC", 0.5, 1 / 6),
        ):
            self.assertAlmostEqual(snapshot[symbol]["weight"], gross_weight, places=5)
            self.assertAlmostEqual(
                snapshot[symbol]["strategy_weight"], strategy_weight, places=5
            )

    def test_dynamic_leverage_multiplies_account_and_configured_symbol_leverage(self) -> None:
        portfolio = Portfolio(
            10_000,
            leverage_multiplier=2,
            allow_fractional_shares=True,
            symbol_leverage_multipliers={"AAA": 1.5, "BBB": 2, "CCC": 1},
        )
        strategy, context, _logs, _intents = self._select_with_scores(
            {
                "buy_top_n": 3,
                "allocation_mode": "leveraged_linear_rank",
                "max_simultaneous_holdings": 3,
            },
            {"AAA": 30, "BBB": 20, "CCC": 10},
            portfolio=portfolio,
        )

        self.assertEqual(
            {
                symbol: float(context.portfolio.effective_leverage(symbol))
                for symbol in context.universe
            },
            {"AAA": 9.0, "BBB": 12.0, "CCC": 6.0},
        )
        type(self)._active_price_scores = context.price_scores
        with patch(
            "services.backtest.code_strategies.calculate_wtme_components",
            side_effect=self._mock_wtme,
        ):
            strategy._select(context)
        self.assertEqual(
            {
                symbol: float(context.portfolio.effective_leverage(symbol))
                for symbol in context.universe
            },
            {"AAA": 9.0, "BBB": 12.0, "CCC": 6.0},
        )

        strategy.params["buy_top_n"] = 1
        with patch(
            "services.backtest.code_strategies.calculate_wtme_components",
            side_effect=self._mock_wtme,
        ):
            strategy._select(context)
        self.assertEqual(
            {
                symbol: float(context.portfolio.effective_leverage(symbol))
                for symbol in context.universe
            },
            {"AAA": 3.0, "BBB": 4.0, "CCC": 2.0},
        )

    def test_score_condition_is_strictly_greater_than_x(self) -> None:
        _strategy, _context, logs, intents = self._select_with_scores(
            {"buy_top_n": 1, "buy_score_threshold": 10},
            {"AAA": 20, "BBB": 10, "CCC": 9},
        )

        self.assertEqual([intent.symbol for intent in intents], ["AAA"])
        by_symbol = {item["symbol"]: item["context"] for item in logs}
        self.assertFalse(by_symbol["BBB"]["passes_score_condition"])
        self.assertFalse(by_symbol["BBB"]["selected_for_target"])

    def test_backtest_filters_rapid_drop_and_buys_highest_remaining_wtme(self) -> None:
        dates = business_dates("2024-01-02", 7)
        trading_date = dates[-1]
        closes = {
            "SPY": [99, 100, 101, 102, 103, 104, 105],
            "GLD": [99, 100, 101, 102, 103, 104, 105],
            "SOXX": [100, 100, 110, 100, 110, 100, 101],
        }
        daily = {
            symbol: daily_rows(dates, values)
            for symbol, values in closes.items()
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in closes}
        for symbol in closes:
            for event, signal in (
                ("09:40", 80.0 if symbol == "SPY" else closes[symbol][-2]),
                ("10:00", closes[symbol][-1]),
            ):
                event_minute = _epoch_minute(trading_date, event)
                for minute_key in (event_minute - 1, event_minute):
                    minute[symbol][minute_key] = {
                        "open": signal,
                        "high": signal,
                        "low": signal,
                        "close": signal,
                    }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=[trading_date],
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        strategy = {
            "name": "WTME 回测集成测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": RapidDropWtmeRotationStrategy.key,
            "code_version": RapidDropWtmeRotationStrategy.version,
            "definition": {
                "symbols": [
                    {"symbol": symbol, "max_weight": 100}
                    for symbol in closes
                ],
                "params": {
                    "wtme_period": 4,
                    "wtme_half_life": 2,
                    "drop_threshold_percent": 10,
                },
            },
            "default_settings": {},
        }
        settings = {
            "start_date": trading_date,
            "end_date": trading_date,
            "initial_capital": 10_000,
            "commission_per_share": 0,
            "minimum_commission": 0,
            "slippage_bps": 0,
            "allow_fractional_shares": True,
            "benchmark": "none",
            "risk_free_rate": 0,
            "strict_data": True,
        }

        result = BacktestEngine(strategy, settings, dataset=dataset).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [("BUY", "GLD")],
        )
        score_logs = [
            log for log in result.logs
            if log["event_type"] == "RAPID_DROP_WTME_DAILY_SCORE"
        ]
        by_symbol = {log["symbol"]: log["context"] for log in score_logs}
        self.assertIn("percent_drop", by_symbol["SPY"]["filter_codes"])
        self.assertIsNone(by_symbol["SPY"]["rank"])
        self.assertEqual(by_symbol["GLD"]["rank"], 1)
        self.assertTrue(by_symbol["GLD"]["selected_for_target"])
        self.assertGreater(by_symbol["GLD"]["score"], by_symbol["SOXX"]["score"])
        self.assertTrue(by_symbol["GLD"]["current_observation_is_partial"])
        self.assertIsNotNone(by_symbol["GLD"]["weighted_return"])
        self.assertIsNotNone(by_symbol["GLD"]["weighted_true_range"])

    def test_visual_strategy_matches_code_strategy_decisions_and_results(self) -> None:
        dates = business_dates("2024-01-02", 10)
        run_dates = dates[-3:]
        closes = {
            "SPY": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
            "GLD": [100, 99, 101, 99, 102, 100, 103, 101, 104, 102],
            "SOXX": [100, 100, 101, 100, 101, 100, 101, 100, 101, 100],
        }
        daily = {
            symbol: daily_rows(dates, values)
            for symbol, values in closes.items()
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in closes}
        selection_prices = [
            {"SPY": 112, "GLD": 102, "SOXX": 101},
            {"SPY": 115, "GLD": 108, "SOXX": 102},
            {"SPY": 118, "GLD": 103, "SOXX": 101},
        ]
        for day_index, trading_date in enumerate(run_dates):
            for symbol in closes:
                previous_close = float(daily[symbol][dates.index(trading_date) - 1]["close"])
                risk_price = (
                    previous_close * 0.75
                    if day_index == 1 and symbol == "SPY"
                    else previous_close
                )
                for event, signal in (
                    ("09:40", risk_price),
                    ("10:00", selection_prices[day_index][symbol]),
                ):
                    event_minute = _epoch_minute(trading_date, event)
                    for minute_key in (event_minute - 1, event_minute):
                        minute[symbol][minute_key] = {
                            "open": signal,
                            "high": signal,
                            "low": signal,
                            "close": signal,
                        }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=run_dates,
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        symbols = [
            {"symbol": symbol, "max_weight": 100, "leverage_multiplier": 2}
            for symbol in closes
        ]
        params = {
            "wtme_period": 4,
            "wtme_half_life": 2,
            "wtme_epsilon": 1e-8,
            "enable_percent_drop_filter": True,
            "drop_threshold_percent": 10,
            "drop_lookback_sessions": 2,
            "risk_check_time": "09:40",
            "selection_time": "10:00",
        }
        code_strategy = {
            "name": "代码 WTME 策略",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": RapidDropWtmeRotationStrategy.key,
            "code_version": RapidDropWtmeRotationStrategy.version,
            "definition": {"symbols": symbols, "params": params},
            "default_settings": {},
        }
        visual_strategy = {
            "name": "非代码 WTME 策略",
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": symbols,
                "rules": [{
                    "id": "rapid-drop-exit",
                    "name": "急跌时退出并当日回避",
                    "enabled": True,
                    "priority": 10,
                    "action": "SELL",
                    "sizing_mode": "TARGET",
                    "value": 0,
                    "condition": "rapid_drop(2, 10) = 1",
                    "when": "09:40",
                }],
                "competition": {
                    "eligibility": "rapid_drop(2, 10) = 0",
                    "eligibility_when": "09:40",
                    "score": "wtme(4, 2, 0.00000001)",
                    "minimum_score": None,
                    "target_weight": 100,
                    "cash_when_none": True,
                    "rebalance_existing": False,
                    "when": "10:00",
                },
            },
            "default_settings": {},
        }
        run_settings = {
            "start_date": run_dates[0],
            "end_date": run_dates[-1],
            "initial_capital": 10_000,
            "leverage_multiplier": 1,
            "commission_per_share": 0.01,
            "minimum_commission": 1,
            "slippage_bps": 0,
            "allow_fractional_shares": True,
            "benchmark": "none",
            "risk_free_rate": 0,
            "strict_data": True,
        }

        code_result = BacktestEngine(
            code_strategy, run_settings, dataset=dataset
        ).run()
        visual_result = BacktestEngine(
            visual_strategy, run_settings, dataset=dataset
        ).run()

        trade_fields = (
            "event_time", "symbol", "side", "quantity",
            "reference_price", "fill_price", "gross_amount", "commission",
            "slippage_amount", "realized_pnl", "position_quantity_after",
            "position_value_after", "position_weight_after", "cash_after",
        )
        self.assertEqual(
            [tuple(trade[field] for field in trade_fields) for trade in visual_result.trades],
            [tuple(trade[field] for field in trade_fields) for trade in code_result.trades],
        )
        self.assertEqual(visual_result.equity_points, code_result.equity_points)
        self.assertEqual(visual_result.metrics, code_result.metrics)


if __name__ == "__main__":
    unittest.main()
