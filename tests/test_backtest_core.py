from __future__ import annotations

from datetime import date, timedelta
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.backtest.code_strategies import RapidDropAtrRotationStrategy
from services.backtest.data import (
    HistoricalDataSet,
    _epoch_minute,
    _validate_bar,
    load_historical_dataset,
)
from services.backtest.dsl import compile_expression
from services.backtest.engine import BacktestEngine
from services.backtest.errors import BacktestValidationError
from services.backtest.metrics import calculate_metrics
from services.backtest.portfolio import OrderIntent, Portfolio
from services.backtest.validation import (
    default_strategy_payload,
    validate_settings,
    validate_strategy_payload,
)
from services.indicator_service import calculate_indicator_values


def business_dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def daily_rows(
    dates: list[str],
    closes: list[float],
    *,
    opens: list[float] | None = None,
) -> list[dict]:
    opens = opens or closes
    return [
        {
            "date": trading_date,
            "open": opens[index],
            "high": max(opens[index], closes[index]) + 1,
            "low": min(opens[index], closes[index]) - 1,
            "close": closes[index],
            "volume": 1000,
        }
        for index, trading_date in enumerate(dates)
    ]


def visual_strategy(
    *,
    rule: dict,
    symbols: list[dict] | None = None,
    selection_mode: str = "single",
) -> dict:
    return {
        "name": "测试策略",
        "design_mode": "visual",
        "selection_mode": selection_mode,
        "definition": {
            "symbols": symbols or [{"symbol": "SPY", "max_weight": 100}],
            "rules": [
                {
                    "id": "test-rule",
                    "name": "测试规则",
                    "enabled": True,
                    "priority": 10,
                    "action": "BUY",
                    "sizing_mode": "TARGET",
                    "value": 100,
                    "condition": "true",
                    "when": "OPEN",
                    **rule,
                }
            ],
        },
        "default_settings": {},
    }


def settings(start: str, end: str, **overrides) -> dict:
    return {
        "start_date": start,
        "end_date": end,
        "initial_capital": 10_000,
        "commission_per_share": 0,
        "minimum_commission": 0,
        "slippage_bps": 0,
        "allow_fractional_shares": False,
        "benchmark": "none",
        "risk_free_rate": 0,
        "strict_data": True,
        **overrides,
    }


class DslSafetyTests(unittest.TestCase):
    def test_expression_supports_boolean_arithmetic_and_history(self) -> None:
        expression = compile_expression(
            "price > ma(20) AND (position <= 0.5 OR close(1) = open(1))"
        )
        self.assertEqual(expression.max_lookback, 20)

    def test_expression_reports_values_used_by_notification_basis(self) -> None:
        expression = compile_expression("ema(8) > ema(13) AND position < 0.5")
        context = SimpleNamespace(
            price=100.0,
            position=0.25,
            resolve_function=lambda name, period: {("ema", 8): 101.5, ("ema", 13): 99.25}[(name, period)],
        )

        self.assertEqual(
            expression.resolve_inputs(context),
            {"position": 0.25, "ema(8)": 101.5, "ema(13)": 99.25},
        )

    def test_expression_supports_all_shared_indicator_functions(self) -> None:
        expression = compile_expression(
            "ratr(14) > 0 AND wtme(40, 15, 0.00000001) >= 0 "
            "AND rapid_drop(5, 5) = 0 AND r_square(25) >= 0"
        )
        context = SimpleNamespace(
            price=100.0,
            position=0.0,
            resolve_function=lambda name, *arguments: {
                ("ratr", 14): 1.25,
                ("wtme", 40, 15, 1e-8): 22.5,
                ("rapid_drop", 5, 5): 0.0,
                ("r_square", 25): 0.86,
            }[(name, *arguments)],
        )

        self.assertTrue(expression.evaluate(context))
        self.assertEqual(expression.max_lookback, 40)
        self.assertEqual(
            expression.resolve_inputs(context),
            {
                "ratr(14)": 1.25,
                "wtme(40,15,1e-08)": 22.5,
                "rapid_drop(5,5)": 0.0,
                "r_square(25)": 0.86,
            },
        )

    def test_expression_rejects_invalid_extended_indicator_parameters(self) -> None:
        for expression in (
            "wtme(1, 15)",
            "wtme(40, 0)",
            "wtme(40, 15, 1)",
            "rapid_drop(5)",
            "rapid_drop(5, 0)",
            "r_square(1)",
            "linear_fit(25)",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(BacktestValidationError):
                    compile_expression(expression)

    def test_expression_rejects_zero_lookback_and_arbitrary_code(self) -> None:
        with self.assertRaises(BacktestValidationError):
            compile_expression("close(0) > 1")
        with self.assertRaises(BacktestValidationError):
            compile_expression("__import__('os').system('whoami')")
        with self.assertRaises(BacktestValidationError):
            compile_expression("price.__class__")

    def test_non_finite_numbers_and_invalid_bars_are_rejected(self) -> None:
        with self.assertRaises(BacktestValidationError):
            validate_settings({"initial_capital": math.nan})
        with self.assertRaises(BacktestValidationError):
            validate_settings({"strict_data": False})
        payload = visual_strategy(
            rule={},
            symbols=[{"symbol": "SPY", "max_weight": math.inf}],
        )
        with self.assertRaises(BacktestValidationError):
            validate_strategy_payload(payload)
        issue = _validate_bar(
            {
                "date": "2024-01-02",
                "open": 10,
                "high": math.inf,
                "low": 9,
                "close": 10,
                "volume": 100,
            },
            symbol="SPY",
            granularity="daily",
        )
        self.assertIn("NaN", issue["reason"])

    def test_leverage_setting_defaults_and_bounds(self) -> None:
        self.assertEqual(validate_settings({})["leverage_multiplier"], 1.0)
        self.assertEqual(
            validate_settings({"leverage_multiplier": 3})["leverage_multiplier"],
            3.0,
        )
        with self.assertRaises(BacktestValidationError):
            validate_settings({"leverage_multiplier": 0.99})
        with self.assertRaises(BacktestValidationError):
            validate_settings({"leverage_multiplier": 10.01})

    def test_symbol_leverage_defaults_and_bounds(self) -> None:
        strategy = default_strategy_payload(
            name="逐标的杠杆测试",
            design_mode="visual",
            selection_mode="single",
        )
        self.assertEqual(
            strategy["definition"]["symbols"][0]["leverage_multiplier"],
            1.0,
        )
        strategy["definition"]["symbols"][0]["leverage_multiplier"] = 2.5
        validated = validate_strategy_payload(strategy)
        self.assertEqual(
            validated["definition"]["symbols"][0]["leverage_multiplier"],
            2.5,
        )
        strategy["definition"]["symbols"][0]["leverage_multiplier"] = 10.01
        with self.assertRaisesRegex(BacktestValidationError, "单标的杠杆"):
            validate_strategy_payload(strategy)

    def test_code_strategy_times_must_be_valid_and_ordered(self) -> None:
        with self.assertRaises(BacktestValidationError):
            RapidDropAtrRotationStrategy.validate_params(
                {"risk_check_time": "25:00"}
            )
        with self.assertRaises(BacktestValidationError):
            RapidDropAtrRotationStrategy.validate_params(
                {
                    "risk_check_time": "10:00",
                    "selection_time": "09:40",
                }
            )
        with self.assertRaisesRegex(BacktestValidationError, "取值不支持"):
            RapidDropAtrRotationStrategy.validate_params(
                {"atr_weighting": "unknown"}
            )

    def test_rapid_drop_atr_weighting_methods(self) -> None:
        rows = [
            {
                "date": "2024-01-01", "open": 100, "high": 100,
                "low": 100, "close": 100, "volume": 1,
            }
        ]
        for day, true_range in enumerate((1.0, 2.0, 3.0, 10.0), start=2):
            rows.append(
                {
                    "date": f"2024-01-0{day}", "open": 100,
                    "high": 100 + true_range / 2,
                    "low": 100 - true_range / 2,
                    "close": 100, "volume": 1,
                }
            )

        values = {
            weighting: RapidDropAtrRotationStrategy._atr_series(
                rows, 3, weighting
            )[-1]
            for weighting in ("wilder", "ema", "linear", "simple")
        }

        self.assertAlmostEqual(values["wilder"], 14 / 3, places=8)
        self.assertAlmostEqual(values["ema"], 6.0, places=8)
        self.assertAlmostEqual(values["linear"], 38 / 6, places=8)
        self.assertAlmostEqual(values["simple"], 5.0, places=8)
        self.assertGreater(values["linear"], values["ema"])
        self.assertGreater(values["ema"], values["simple"])
        self.assertGreater(values["simple"], values["wilder"])


class DataPreflightTests(unittest.TestCase):
    def _session(self) -> dict:
        return {
            "trading_date": "2024-01-02",
            "open_minute_utc": _epoch_minute("2024-01-02", "09:30"),
            "close_minute_utc": _epoch_minute("2024-01-02", "16:00"),
            "is_early_close": False,
        }

    @patch("services.backtest.data.repository.get_symbol", return_value={
        "asset_class": "crypto",
        "quantity_step": 0.0001,
    })
    @patch("services.backtest.data.ensure_corporate_actions", return_value=[])
    @patch("services.backtest.data.ensure_market_sessions")
    @patch("services.backtest.data.repository.get_strategy_daily_prices")
    def test_us_strategy_daily_history_excludes_weekend_and_holiday(
        self,
        get_daily,
        get_sessions,
        _actions,
        _symbol,
    ) -> None:
        get_daily.return_value = daily_rows(
            ["2023-12-29", "2023-12-30", "2024-01-01", "2024-01-02"],
            [100, 900, 800, 103],
        )
        get_sessions.return_value = [
            {
                "trading_date": day,
                "open_minute_utc": _epoch_minute(day, "09:30"),
                "close_minute_utc": _epoch_minute(day, "16:00"),
                "is_early_close": False,
            }
            for day in ("2023-12-29", "2024-01-02")
        ]

        dataset = load_historical_dataset(
            universe=["BTC/USD"],
            additional_symbols=[],
            start_date="2024-01-02",
            end_date="2024-01-02",
            intraday_events=[],
            minimum_lookback=1,
            market={"type": "US_EQUITY"},
        )

        self.assertEqual(
            [row["date"] for row in dataset.daily["BTC/USD"]],
            ["2023-12-29", "2024-01-02"],
        )
        self.assertEqual(
            dataset.manifest["symbols"]["BTC/USD"]["daily_series"],
            "US_EQUITY_SESSION",
        )

    @patch(
        "services.backtest.data.ensure_market_sessions"
    )
    @patch(
        "services.backtest.data.repository.get_daily_prices"
    )
    def test_incomplete_daily_bar_fails_closed(
        self,
        get_daily,
        get_sessions,
    ) -> None:
        get_sessions.return_value = [self._session()]
        rows = daily_rows(
            ["2024-01-01", "2024-01-02"],
            [10, 11],
        )
        rows[-1]["is_complete"] = 0
        get_daily.return_value = rows

        with self.assertRaisesRegex(Exception, "日线数据"):
            load_historical_dataset(
                universe=["SPY"],
                additional_symbols=[],
                start_date="2024-01-02",
                end_date="2024-01-02",
                intraday_events=[],
                minimum_lookback=1,
            )

    @patch(
        "services.backtest.data.ensure_corporate_actions",
        return_value=[],
    )
    @patch(
        "services.backtest.data.ensure_market_sessions"
    )
    @patch(
        "services.backtest.data.repository.get_daily_prices"
    )
    def test_loaded_snapshot_has_reproducible_hashes(
        self,
        get_daily,
        get_sessions,
        _get_actions,
    ) -> None:
        get_sessions.return_value = [self._session()]
        rows = daily_rows(
            ["2024-01-01", "2024-01-02"],
            [10, 11],
        )
        for row in rows:
            row["is_complete"] = 1
        get_daily.return_value = rows

        dataset = load_historical_dataset(
            universe=["SPY"],
            additional_symbols=[],
            start_date="2024-01-02",
            end_date="2024-01-02",
            intraday_events=[],
            minimum_lookback=1,
        )

        self.assertEqual(len(dataset.manifest["symbols"]["SPY"]["daily_sha256"]), 64)
        self.assertEqual(len(dataset.manifest["market_calendar_sha256"]), 64)


class PortfolioAccountingTests(unittest.TestCase):
    def test_leverage_scales_exposure_and_uses_negative_cash(self) -> None:
        portfolio = Portfolio(1000, leverage_multiplier=3)
        trade = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 100, "leveraged buy"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )

        self.assertEqual(trade["quantity"], 300)
        self.assertEqual(float(portfolio.cash), -2000)
        self.assertEqual(float(portfolio.borrowed_cash), 2000)
        self.assertAlmostEqual(float(portfolio.weight("SPY", {"SPY": 10})), 1)
        self.assertAlmostEqual(float(portfolio.gross_leverage({"SPY": 10})), 3)

    def test_account_and_symbol_leverage_multiply_without_double_counting(self) -> None:
        portfolio = Portfolio(
            1000,
            leverage_multiplier=2,
            symbol_leverage_multipliers={"SPY": 3},
        )
        buy = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 50, "combined leverage"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )

        self.assertEqual(buy["quantity"], 300)
        self.assertEqual(float(portfolio.effective_leverage("SPY")), 6)
        self.assertAlmostEqual(float(portfolio.weight("SPY", {"SPY": 10})), 0.5)
        self.assertAlmostEqual(
            portfolio.snapshot({"SPY": 10})["SPY"]["strategy_weight"],
            0.5,
        )

        sell = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 25, "reduce normalized weight"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-03 OPEN",
        )
        self.assertEqual(sell["quantity"], 150)
        self.assertAlmostEqual(float(portfolio.weight("SPY", {"SPY": 10})), 0.25)

    def test_different_symbol_leverages_keep_independent_normalized_weights(self) -> None:
        portfolio = Portfolio(
            1000,
            leverage_multiplier=2,
            symbol_leverage_multipliers={"SPY": 1, "GLD": 3},
        )
        portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 50, "SPY target"),
            reference_price=10,
            marks={"SPY": 10, "GLD": 10},
            max_weight_percent=50,
            event_time="2024-01-02 OPEN",
        )
        portfolio.execute(
            OrderIntent("GLD", "BUY", "TARGET", 50, "GLD target"),
            reference_price=10,
            marks={"SPY": 10, "GLD": 10},
            max_weight_percent=50,
            event_time="2024-01-02 OPEN",
        )

        self.assertEqual(float(portfolio.quantity("SPY")), 100)
        self.assertEqual(float(portfolio.quantity("GLD")), 300)
        self.assertAlmostEqual(float(portfolio.weight("SPY", {"SPY": 10, "GLD": 10})), 0.5)
        self.assertAlmostEqual(float(portfolio.weight("GLD", {"SPY": 10, "GLD": 10})), 0.5)
        self.assertAlmostEqual(float(portfolio.gross_leverage({"SPY": 10, "GLD": 10})), 4)

    def test_round_trip_cash_and_fifo_pnl_include_both_commissions(self) -> None:
        portfolio = Portfolio(
            1000,
            commission_per_share=0,
            minimum_commission=1,
            slippage_bps=0,
        )
        buy = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 100, "buy"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )
        sell = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 0, "sell"),
            reference_price=12,
            marks={"SPY": 12},
            event_time="2024-01-03 OPEN",
        )

        self.assertEqual(buy["quantity"], 99)
        self.assertAlmostEqual(float(portfolio.cash), 1196.0, places=8)
        self.assertAlmostEqual(sell["realized_pnl"], 196.0, places=8)
        self.assertAlmostEqual(float(portfolio.total_commission), 2.0, places=8)

    def test_slippage_changes_fill_cash_and_realized_pnl_exactly(self) -> None:
        portfolio = Portfolio(
            1000,
            commission_per_share=0.01,
            minimum_commission=1,
            slippage_bps=100,
        )
        buy = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 50, "buy"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 10:00",
        )
        sell = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 0, "sell"),
            reference_price=12,
            marks={"SPY": 12},
            event_time="2024-01-03 10:00",
        )

        self.assertEqual(buy["quantity"], 49)
        self.assertAlmostEqual(buy["fill_price"], 10.1, places=8)
        self.assertAlmostEqual(sell["fill_price"], 11.88, places=8)
        self.assertAlmostEqual(float(portfolio.cash), 1085.22, places=8)
        self.assertAlmostEqual(sell["realized_pnl"], 85.22, places=8)
        self.assertAlmostEqual(float(portfolio.total_slippage), 10.78, places=8)

    def test_invalid_negative_target_is_rejected_instead_of_clipped(self) -> None:
        portfolio = Portfolio(1000)
        with self.assertRaises(Exception):
            portfolio.execute(
                OrderIntent("SPY", "SELL", "DELTA", 10, "invalid"),
                reference_price=10,
                marks={"SPY": 10},
                event_time="2024-01-02 OPEN",
            )

    def test_commission_adjusted_quantity_never_exceeds_target_weight(self) -> None:
        portfolio = Portfolio(1000, minimum_commission=1)
        trade = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 50, "target"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )
        self.assertEqual(trade["quantity"], 49)
        self.assertLessEqual(trade["position_weight_after"], 0.5)

    def test_risk_metrics_include_first_session_return_from_initial_cash(self) -> None:
        points = [
            {
                "trading_date": "2024-01-02",
                "equity": 90,
                "positions_value": 90,
                "benchmark_return_rate": None,
            },
            {
                "trading_date": "2024-01-03",
                "equity": 90,
                "positions_value": 90,
                "benchmark_return_rate": None,
            },
        ]

        metrics = calculate_metrics(
            points,
            [],
            initial_capital=100,
        )

        self.assertGreater(metrics["annualized_volatility"], 0)
        self.assertAlmostEqual(metrics["max_drawdown"], 0.1, places=12)

    def test_fractional_binary_sizing_handles_large_minimum_commission(self) -> None:
        portfolio = Portfolio(
            1000,
            minimum_commission=100,
            allow_fractional_shares=True,
        )
        trade = portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 100, "target"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )
        self.assertAlmostEqual(trade["quantity"], 90, places=6)
        self.assertAlmostEqual(float(portfolio.cash), 0, places=6)

    def test_sell_target_accounts_for_commission_reducing_equity(self) -> None:
        portfolio = Portfolio(1000, minimum_commission=100)
        portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 100, "buy"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-02 OPEN",
        )

        trade = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 50, "rebalance"),
            reference_price=10,
            marks={"SPY": 10},
            event_time="2024-01-03 OPEN",
        )

        self.assertEqual(trade["quantity"], 50)
        self.assertLessEqual(trade["position_weight_after"], 0.5)


class EngineTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = business_dates("2023-11-01", 25)
        self.run_dates = self.dates[-3:]
        closes = [10.0] * 21 + [11.0, 12.0, 13.0, 14.0]
        opens = [10.0] * 22 + [20.0, 21.0, 22.0]
        self.daily = daily_rows(self.dates, closes, opens=opens)

    def test_open_rule_reads_previous_close_and_fills_current_open(self) -> None:
        dataset = HistoricalDataSet(
            daily={"SPY": self.daily},
            sessions=self.run_dates,
        )
        strategy = visual_strategy(
            rule={"condition": "price > ma(20)", "when": "OPEN"}
        )

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0]["reference_price"], 20.0)
        self.assertEqual(result.trades[0]["fill_price"], 20.0)
        self.assertEqual(result.trades[0]["quantity"], 500)

    def test_visual_strategy_combines_account_and_symbol_leverage(self) -> None:
        trading_date = self.run_dates[0]
        dataset = HistoricalDataSet(
            daily={"SPY": daily_rows(self.dates, [100.0] * len(self.dates))},
            sessions=[trading_date],
        )
        strategy = visual_strategy(
            rule={"condition": "true", "when": "OPEN"},
            symbols=[
                {
                    "symbol": "SPY",
                    "max_weight": 100,
                    "leverage_multiplier": 3,
                }
            ],
        )

        result = BacktestEngine(
            strategy,
            settings(trading_date, trading_date, leverage_multiplier=2),
            dataset=dataset,
        ).run()

        self.assertEqual(result.trades[0]["quantity"], 600)
        self.assertAlmostEqual(result.equity_points[0]["gross_leverage"], 6)
        self.assertAlmostEqual(
            result.equity_points[0]["positions"]["SPY"]["strategy_weight"],
            1,
        )

    def test_close_signal_never_fills_same_close(self) -> None:
        dataset = HistoricalDataSet(
            daily={"SPY": self.daily},
            sessions=self.run_dates,
        )
        strategy = visual_strategy(
            rule={"condition": "price > close(1)", "when": "CLOSE"}
        )

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0]["event_time"], f"{self.run_dates[1]} 09:30 America/New_York")
        self.assertEqual(result.trades[0]["reference_price"], 21.0)
        self.assertNotEqual(result.trades[0]["reference_price"], 13.0)

    def test_leveraged_intraday_liquidation_stops_and_returns_results(self) -> None:
        trading_date = self.run_dates[0]
        daily = daily_rows(self.dates, [100.0] * len(self.dates))
        daily[-3].update({"open": 100, "high": 101, "low": 20, "close": 20})
        minute = _epoch_minute(trading_date, "09:31")
        dataset = HistoricalDataSet(
            daily={"SPY": daily},
            sessions=[trading_date],
            minute={
                "SPY": {
                    minute: {
                        "minute_utc": minute,
                        "open": 100,
                        "high": 100,
                        "low": 20,
                        "close": 20,
                    }
                }
            },
        )
        strategy = visual_strategy(rule={"condition": "true", "when": "OPEN"})

        result = BacktestEngine(
            strategy,
            settings(
                trading_date,
                trading_date,
                leverage_multiplier=3,
            ),
            dataset=dataset,
        ).run()

        self.assertEqual(result.termination_reason, "LIQUIDATED")
        self.assertTrue(result.metrics["liquidated"])
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[-1]["reason"], "账户爆仓强制平仓")
        self.assertLess(result.metrics["ending_equity"], 0)
        self.assertIsNone(result.metrics["annualized_return"])
        self.assertIsNone(result.metrics["sharpe_ratio"])
        self.assertIsNone(result.metrics["sortino_ratio"])
        self.assertAlmostEqual(result.metrics["max_gross_leverage"], 3)
        self.assertEqual(result.equity_points[-1]["positions"], {})
        self.assertTrue(any(log["event_type"] == "LIQUIDATION" for log in result.logs))

    def test_intraday_signal_uses_previous_minute_and_current_open(self) -> None:
        first = self.run_dates[0]
        previous = _epoch_minute(first, "09:40") - 1
        current = previous + 1
        minute = {
            previous: {
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
            },
            current: {
                "open": 20,
                "high": 999,
                "low": 1,
                "close": 999,
            },
        }
        dataset = HistoricalDataSet(
            daily={"SPY": self.daily},
            sessions=[first],
            minute={"SPY": minute},
            required_intraday_events=["09:40"],
        )
        strategy = visual_strategy(
            rule={"condition": "price = 11", "when": "09:40"}
        )

        result = BacktestEngine(
            strategy,
            settings(first, first),
            dataset=dataset,
        ).run()

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0]["reference_price"], 20.0)
        self.assertEqual(result.trades[0]["quantity"], 500)

    def test_explicit_0930_requires_minute_data_unlike_open(self) -> None:
        dataset = HistoricalDataSet(
            daily={"SPY": self.daily},
            sessions=self.run_dates,
        )
        strategy = visual_strategy(
            rule={"condition": "true", "when": "09:30"}
        )

        with self.assertRaisesRegex(Exception, "上一分钟行情"):
            BacktestEngine(
                strategy,
                settings(self.run_dates[0], self.run_dates[-1]),
                dataset=dataset,
            ).run()

    def test_changing_current_minute_future_values_does_not_change_order(self) -> None:
        first = self.run_dates[0]
        previous = _epoch_minute(first, "09:40") - 1
        current = previous + 1
        trades = []
        for future_close in (21, 9999):
            dataset = HistoricalDataSet(
                daily={"SPY": self.daily},
                sessions=[first],
                minute={
                    "SPY": {
                        previous: {"open": 10, "high": 11, "low": 9, "close": 11},
                        current: {
                            "open": 20,
                            "high": future_close,
                            "low": 1,
                            "close": future_close,
                        },
                    }
                },
                required_intraday_events=["09:40"],
            )
            strategy = visual_strategy(
                rule={"condition": "price > 10", "when": "09:40"}
            )
            result = BacktestEngine(
                strategy,
                settings(first, first),
                dataset=dataset,
            ).run()
            trades.append(result.trades)

        self.assertEqual(trades[0], trades[1])

    def test_intraday_r_square_uses_signal_price_without_future_daily_close(self) -> None:
        trading_date = self.run_dates[0]
        previous_minute = _epoch_minute(trading_date, "14:00") - 1
        current_minute = previous_minute + 1
        daily = [dict(row) for row in self.daily]
        current_day = next(row for row in daily if row["date"] == trading_date)
        current_day.update({"open": 20.0, "high": 500.0, "low": 1.0, "close": 500.0})
        dataset = HistoricalDataSet(
            daily={"SPY": daily},
            sessions=[trading_date],
            minute={
                "SPY": {
                    previous_minute: {
                        "open": 11.5, "high": 12.2, "low": 11.4, "close": 12.1,
                    },
                    current_minute: {
                        "open": 20.0, "high": 999.0, "low": 1.0, "close": 999.0,
                    },
                }
            },
            required_intraday_events=["14:00"],
        )

        event_price = dataset.event_price("SPY", trading_date, "14:00")
        context = dataset.expression_context(
            symbol="SPY",
            trading_date=trading_date,
            event="14:00",
            price=event_price.signal_price,
            position=0,
        )
        completed = dataset.daily_before("SPY", trading_date)
        synthetic_current = {
            "date": trading_date,
            "open": completed[-1]["close"],
            "high": max(completed[-1]["close"], 12.1),
            "low": min(completed[-1]["close"], 12.1),
            "close": 12.1,
            "volume": 0,
            "is_complete": 0,
        }
        expected = calculate_indicator_values(
            [*completed, synthetic_current], "LINEAR_FIT", 2
        )[-1]
        leaked = calculate_indicator_values(
            dataset.indicator_history(
                "SPY", trading_date, include_current=True
            ),
            "LINEAR_FIT",
            2,
        )[-1]

        self.assertEqual(event_price.signal_price, 12.1)
        self.assertAlmostEqual(context.resolve_function("r_square", 2), expected)
        self.assertNotAlmostEqual(expected, leaked, places=6)


class CorporateActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = business_dates("2023-11-01", 24)
        self.run_dates = self.dates[-3:]

    def test_forward_split_adjusts_position_and_history_without_equity_jump(self) -> None:
        closes = [100.0] * 22 + [50.0, 50.0]
        opens = list(closes)
        action = {
            "provider_id": "split-1",
            "action_type": "forward_split",
            "symbol": "SPY",
            "process_date": self.run_dates[1],
            "ex_date": self.run_dates[1],
            "old_rate": 1,
            "new_rate": 2,
        }
        dataset = HistoricalDataSet(
            daily={"SPY": daily_rows(self.dates, closes, opens=opens)},
            sessions=self.run_dates,
            corporate_actions=[action],
        )
        strategy = visual_strategy(rule={"condition": "true", "when": "OPEN"})

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        second = result.equity_points[1]
        self.assertEqual(second["positions"]["SPY"]["quantity"], 200)
        self.assertAlmostEqual(second["equity"], 10_000, places=8)
        context = dataset.expression_context(
            symbol="SPY",
            trading_date=self.run_dates[1],
            event="OPEN",
            price=50,
            position=1,
        )
        self.assertAlmostEqual(context.resolve_function("close", 1), 50, places=8)

    def test_cash_dividend_is_receivable_then_cash_without_equity_loss(self) -> None:
        closes = [100.0] * 22 + [99.0, 99.0]
        opens = list(closes)
        action = {
            "provider_id": "dividend-1",
            "action_type": "cash_dividend",
            "symbol": "SPY",
            "process_date": self.run_dates[1],
            "ex_date": self.run_dates[1],
            "payable_date": self.run_dates[2],
            "cash_rate": 1,
        }
        dataset = HistoricalDataSet(
            daily={"SPY": daily_rows(self.dates, closes, opens=opens)},
            sessions=self.run_dates,
            corporate_actions=[action],
        )
        strategy = visual_strategy(rule={"condition": "true", "when": "OPEN"})

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertAlmostEqual(result.equity_points[1]["receivables"], 100, places=8)
        self.assertAlmostEqual(result.equity_points[1]["equity"], 10_000, places=8)
        self.assertAlmostEqual(result.equity_points[2]["receivables"], 0, places=8)
        self.assertAlmostEqual(result.equity_points[2]["cash"], 1, places=8)
        self.assertEqual(
            result.equity_points[2]["positions"]["SPY"]["quantity"],
            101,
        )
        self.assertAlmostEqual(result.equity_points[2]["equity"], 10_000, places=8)

    def test_reverse_split_fraction_fails_without_cash_in_lieu_data(self) -> None:
        portfolio = Portfolio(1000, allow_fractional_shares=False)
        portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 100, "buy"),
            reference_price=30,
            marks={"SPY": 30},
            event_time="2024-01-02 OPEN",
        )

        with self.assertRaisesRegex(Exception, "现金替代"):
            portfolio.apply_corporate_actions(
                [
                    {
                        "symbol": "SPY",
                        "action_type": "reverse_split",
                        "old_rate": 10,
                        "new_rate": 1,
                    }
                ],
                trading_date="2024-01-03",
            )


class StrategyModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = business_dates("2023-10-02", 12)
        self.run_dates = self.dates[-2:]

    def test_distribution_allocates_independent_capped_positions(self) -> None:
        dataset = HistoricalDataSet(
            daily={
                "SPY": daily_rows(self.dates, [10.0] * len(self.dates)),
                "GLD": daily_rows(self.dates, [20.0] * len(self.dates)),
            },
            sessions=self.run_dates,
        )
        strategy = visual_strategy(
            rule={"condition": "true", "value": 50},
            symbols=[
                {"symbol": "SPY", "max_weight": 50},
                {"symbol": "GLD", "max_weight": 50},
            ],
            selection_mode="distribution",
        )

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        positions = result.equity_points[0]["positions"]
        self.assertEqual(positions["SPY"]["quantity"], 500)
        self.assertEqual(positions["GLD"]["quantity"], 250)
        self.assertAlmostEqual(positions["SPY"]["weight"], 0.5, places=8)
        self.assertAlmostEqual(positions["GLD"]["weight"], 0.5, places=8)

    def test_competition_sells_old_winner_before_buying_new_winner(self) -> None:
        spy_closes = [10.0] * 10 + [30.0, 30.0]
        gld_closes = [20.0] * len(self.dates)
        dataset = HistoricalDataSet(
            daily={
                "SPY": daily_rows(self.dates, spy_closes),
                "GLD": daily_rows(self.dates, gld_closes),
            },
            sessions=self.run_dates,
        )
        strategy = {
            "name": "竞争测试",
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": [
                    {"symbol": "SPY", "max_weight": 100},
                    {"symbol": "GLD", "max_weight": 100},
                ],
                "rules": [
                    {
                        "id": "never-hold",
                        "name": "不触发风险规则",
                        "enabled": True,
                        "priority": 10,
                        "action": "HOLD",
                        "sizing_mode": "TARGET",
                        "value": 0,
                        "condition": "false",
                        "when": "OPEN",
                    }
                ],
                "competition": {
                    "eligibility": "true",
                    "score": "price",
                    "target_weight": 100,
                    "cash_when_none": True,
                    "when": "OPEN",
                },
            },
            "default_settings": {},
        }

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [("BUY", "GLD"), ("SELL", "GLD"), ("BUY", "SPY")],
        )
        self.assertGreaterEqual(
            result.trades[2]["event_time"],
            result.trades[1]["event_time"],
        )

    def test_competition_accepts_no_optional_rules_and_enforces_minimum_score(self) -> None:
        strategy = {
            "name": "无普通规则的竞争策略",
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": [
                    {"symbol": "SPY", "max_weight": 100},
                    {"symbol": "GLD", "max_weight": 100},
                ],
                "rules": [],
                "competition": {
                    "eligibility": "true",
                    "score": "-1",
                    "minimum_score": 0,
                    "target_weight": 100,
                    "cash_when_none": True,
                    "when": "OPEN",
                },
            },
            "default_settings": {},
        }
        validated = validate_strategy_payload(strategy)
        dataset = HistoricalDataSet(
            daily={
                "SPY": daily_rows(self.dates, [10.0] * len(self.dates)),
                "GLD": daily_rows(self.dates, [20.0] * len(self.dates)),
            },
            sessions=self.run_dates,
        )

        result = BacktestEngine(
            validated,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertEqual(validated["definition"]["rules"], [])
        self.assertEqual(result.trades, [])
        score_logs = [
            log for log in result.logs
            if log["event_type"] == "COMPETITION_SCORE"
        ]
        self.assertTrue(score_logs)
        self.assertTrue(all(
            not log["context"]["passes_minimum_score"]
            for log in score_logs
        ))

    def test_noncompetition_visual_strategy_still_requires_an_enabled_rule(self) -> None:
        strategy = visual_strategy(rule={})
        strategy["definition"]["rules"] = []

        with self.assertRaisesRegex(BacktestValidationError, "至少需要一条启用的规则"):
            validate_strategy_payload(strategy)

    def test_competition_does_not_override_partial_risk_sell(self) -> None:
        spy_closes = [10.0] * 10 + [30.0, 30.0]
        gld_closes = [20.0] * 10 + [10.0, 10.0]
        dataset = HistoricalDataSet(
            daily={
                "SPY": daily_rows(self.dates, spy_closes),
                "GLD": daily_rows(self.dates, gld_closes),
            },
            sessions=self.run_dates,
        )
        strategy = {
            "name": "竞争部分风控测试",
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": [
                    {"symbol": "SPY", "max_weight": 100},
                    {"symbol": "GLD", "max_weight": 100},
                ],
                "rules": [
                    {
                        "id": "partial-risk",
                        "name": "急跌减仓",
                        "enabled": True,
                        "priority": 10,
                        "action": "SELL",
                        "sizing_mode": "TARGET",
                        "value": 50,
                        "condition": "price < close(2)",
                        "when": "OPEN",
                    }
                ],
                "competition": {
                    "eligibility": "true",
                    "score": "price",
                    "target_weight": 100,
                    "cash_when_none": True,
                    "when": "OPEN",
                },
            },
            "default_settings": {},
        }

        result = BacktestEngine(
            strategy,
            settings(self.run_dates[0], self.run_dates[-1]),
            dataset=dataset,
        ).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [("BUY", "GLD"), ("SELL", "GLD")],
        )
        self.assertEqual(result.trades[-1]["quantity"], 500)
        self.assertEqual(result.trades[-1]["position_weight_after"], 0.5)
        self.assertEqual(
            set(result.equity_points[-1]["positions"]),
            {"GLD"},
        )

    def test_code_strategy_risk_exit_then_rotates_to_next_winner(self) -> None:
        symbols = ["SPY", "GLD", "NVDA", "MU", "XLE"]
        daily = {
            symbol: daily_rows(self.dates, [100.0] * len(self.dates))
            for symbol in symbols
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in symbols}
        first, second = self.run_dates
        scores_day_one = {"SPY": 101, "GLD": 102, "NVDA": 110, "MU": 103, "XLE": 104}
        scores_day_two = {"SPY": 101, "GLD": 102, "NVDA": 95, "MU": 103, "XLE": 108}
        for trading_date, scores, nvda_risk in (
            (first, scores_day_one, 100),
            (second, scores_day_two, 90),
        ):
            for symbol in symbols:
                for event, signal in (
                    ("09:40", nvda_risk if symbol == "NVDA" else 100),
                    ("10:00", scores[symbol]),
                ):
                    current = _epoch_minute(trading_date, event)
                    minute[symbol][current - 1] = {
                        "open": signal,
                        "high": signal,
                        "low": signal,
                        "close": signal,
                    }
                    minute[symbol][current] = {
                        "open": signal,
                        "high": signal,
                        "low": signal,
                        "close": signal,
                    }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=self.run_dates,
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        strategy = {
            "name": "代码策略测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": "rapid_drop_atr_rotation",
            "code_version": "1.3.0",
            "definition": {
                "symbols": [
                    {"symbol": symbol, "max_weight": 100}
                    for symbol in symbols
                ],
                "params": {},
            },
            "default_settings": {},
        }

        result = BacktestEngine(
            strategy,
            settings(first, second),
            dataset=dataset,
        ).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [
                ("BUY", "NVDA"),
                ("SELL", "NVDA"),
                ("BUY", "XLE"),
            ],
        )
        self.assertIn("09:40", result.trades[1]["event_time"])
        self.assertIn("10:00", result.trades[2]["event_time"])
        nvda_risk_log = next(
            log
            for log in result.logs
            if log["event_type"] == "RAPID_DROP_ATR_DAILY_SCORE"
            and log["event_time"].startswith(second)
            and log["symbol"] == "NVDA"
        )
        self.assertIn("percent_drop", nvda_risk_log["context"]["filter_codes"])
        self.assertAlmostEqual(
            nvda_risk_log["context"]["score"], (95 - 100) / 2, places=8
        )
        self.assertAlmostEqual(
            nvda_risk_log["context"]["percent_changes"][-1], -0.1, places=8
        )
        self.assertEqual(nvda_risk_log["context"]["risk_event_price"], 90)
        self.assertIn("10:00", nvda_risk_log["context"]["score_formula"])

        strategy["definition"]["params"] = {
            "enable_percent_drop_filter": False,
            "enable_atr_drop_filter": True,
            "drop_threshold_atr": 4.0,
        }
        atr_result = BacktestEngine(
            strategy,
            settings(first, second),
            dataset=dataset,
        ).run()
        atr_nvda_log = next(
            log
            for log in atr_result.logs
            if log["event_type"] == "RAPID_DROP_ATR_DAILY_SCORE"
            and log["event_time"].startswith(second)
            and log["symbol"] == "NVDA"
        )
        self.assertNotIn("percent_drop", atr_nvda_log["context"]["filter_codes"])
        self.assertIn("atr_drop", atr_nvda_log["context"]["filter_codes"])

    def test_rapid_drop_strategy_validates_holdings_against_candidate_count(self) -> None:
        with self.assertRaisesRegex(
            BacktestValidationError, "目标持仓数量不能超过候选池"
        ):
            RapidDropAtrRotationStrategy.validate_definition(
                {
                    "symbols": [{"symbol": "SPY", "max_weight": 100}],
                    "params": {"holdings_num": 2},
                }
            )

    def test_rapid_drop_strategy_holds_top_two_at_equal_target_weights(self) -> None:
        symbols = ["SPY", "GLD", "SOXX"]
        prices = {"SPY": 101.0, "GLD": 103.0, "SOXX": 110.0}
        daily = {
            symbol: daily_rows(
                self.dates,
                [100.0] * (len(self.dates) - 2) + [prices[symbol], 100.0],
            )
            for symbol in symbols
        }
        trading_date = self.run_dates[0]
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in symbols}
        for symbol in symbols:
            for event, signal in (("09:40", 100.0), ("10:00", prices[symbol])):
                current = _epoch_minute(trading_date, event)
                for minute_key in (current - 1, current):
                    minute[symbol][minute_key] = {
                        "open": signal, "high": signal,
                        "low": signal, "close": signal,
                    }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=[trading_date],
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        strategy = {
            "name": "代码多持仓测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": "rapid_drop_atr_rotation",
            "code_version": "1.3.0",
            "definition": {
                "symbols": [
                    {"symbol": symbol, "max_weight": 100}
                    for symbol in symbols
                ],
                "params": {
                    "holdings_num": 2,
                    "enable_percent_drop_filter": False,
                    "enable_atr_drop_filter": False,
                },
            },
            "default_settings": {},
        }

        result = BacktestEngine(
            strategy,
            settings(trading_date, trading_date, allow_fractional_shares=True),
            dataset=dataset,
        ).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [("BUY", "SOXX"), ("BUY", "GLD")],
        )
        positions = result.equity_points[0]["positions"]
        self.assertEqual(set(positions), {"SOXX", "GLD"})
        self.assertAlmostEqual(positions["SOXX"]["weight"], 0.5, places=7)
        self.assertAlmostEqual(positions["GLD"]["weight"], 0.5, places=7)

    def test_rapid_drop_leverage_does_not_rebalance_unchanged_winner_daily(self) -> None:
        symbols = ["SPY", "GLD"]
        first, second = self.run_dates[:2]
        daily = {
            symbol: daily_rows(self.dates, [100.0] * len(self.dates))
            for symbol in symbols
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in symbols}
        for trading_date in (first, second):
            for symbol in symbols:
                for event, signal in (
                    ("09:40", 100.0),
                    ("10:00", 110.0 if symbol == "SPY" else 100.0),
                ):
                    current = _epoch_minute(trading_date, event)
                    for minute_key in (current - 1, current):
                        minute[symbol][minute_key] = {
                            "open": signal,
                            "high": signal,
                            "low": signal,
                            "close": signal,
                        }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=[first, second],
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        strategy = {
            "name": "杠杆轮动不重复交易测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": "rapid_drop_atr_rotation",
            "code_version": "1.3.0",
            "definition": {
                "symbols": [
                    {"symbol": symbol, "max_weight": 100}
                    for symbol in symbols
                ],
                "params": {
                    "enable_percent_drop_filter": False,
                    "enable_atr_drop_filter": False,
                },
            },
            "default_settings": {},
        }

        result = BacktestEngine(
            strategy,
            settings(
                first,
                second,
                leverage_multiplier=2,
                allow_fractional_shares=True,
            ),
            dataset=dataset,
        ).run()

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0]["symbol"], "SPY")


if __name__ == "__main__":
    unittest.main()
