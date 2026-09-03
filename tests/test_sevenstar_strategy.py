from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

from services.backtest.code_strategies import SevenStarEtfRotationStrategy
from services.backtest.data import EventPrice, HistoricalDataSet, load_historical_dataset
from services.backtest.engine import BacktestEngine
from services.backtest.errors import BacktestDataError
from services.backtest.portfolio import OrderIntent, Portfolio
from services.backtest.presets import SEVENSTAR_LARGE, SEVENSTAR_SMALL


NEW_YORK = ZoneInfo("America/New_York")


def epoch_minute(day: str, hhmm: str) -> int:
    local = datetime.combine(
        date.fromisoformat(day), time.fromisoformat(hhmm), tzinfo=NEW_YORK
    )
    return int(local.astimezone(timezone.utc).timestamp()) // 60


def rows(start: date, count: int, base: float, growth: float) -> list[dict]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            close = base * (growth ** len(result))
            result.append(
                {
                    "date": current.isoformat(),
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1_000_000,
                    "is_complete": 1,
                }
            )
        current += timedelta(days=1)
    return result


class SevenStarAlgorithmTests(unittest.TestCase):
    def test_retired_minimum_trade_value_is_ignored_for_old_snapshots(self) -> None:
        values = SevenStarEtfRotationStrategy.validate_params({
            "minimum_trade_value_usd": 0,
        })

        self.assertNotIn("minimum_trade_value_usd", values)

    def test_rebalance_target_uses_combined_symbol_leverage(self) -> None:
        strategy = SevenStarEtfRotationStrategy(
            {
                "enable_profit_protection": False,
                "rebalance_tolerance_percent": 0,
            }
        )
        portfolio = Portfolio(
            1000,
            leverage_multiplier=2,
            symbol_leverage_multipliers={"AAA": 3},
        )
        portfolio.execute(
            OrderIntent("AAA", "BUY", "TARGET", 50, "initial"),
            reference_price=10,
            marks={"AAA": 10},
            event_time="2024-01-01 OPEN",
        )
        context = SimpleNamespace(
            portfolio=portfolio,
            marks={"AAA": 10},
            all_candidate_symbols=["AAA"],
            event_prices={"AAA": SimpleNamespace(signal_price=10)},
            universe=["AAA"],
        )

        intents = strategy._buy_intents(context, [{"etf": "AAA"}])

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, "BUY")
        self.assertEqual(intents[0].value_percent, 100)

    def test_pool_contents_match_product_definition(self) -> None:
        self.assertEqual(SevenStarEtfRotationStrategy.version, "1.2.0")
        self.assertEqual(SEVENSTAR_SMALL, ["GLD", "USO", "SPY", "QQQ", "DIA", "IWM", "TLT"])
        self.assertEqual(len(SEVENSTAR_LARGE), 41)
        for symbol in ("COPX", "MAGS", "EWY", "IGV", "UFOX", "TLT", "CWB", "BTC/USD"):
            self.assertIn(symbol, SEVENSTAR_LARGE)
        self.assertNotIn("VGK", SEVENSTAR_LARGE)

    def test_weighted_regression_uses_consistent_effective_weights(self) -> None:
        prices = [100.0, 101.5, 102.0, 104.2, 105.8, 108.1]
        annualized, r_squared, score = SevenStarEtfRotationStrategy._weighted_trend(
            prices, 5
        )

        y = np.log(np.asarray(prices))
        x = np.arange(len(y))
        weights = np.linspace(1, 2, len(y))
        importance = weights ** 2
        expected_mean = np.average(y, weights=importance)
        centered_y = y - expected_mean
        slope, intercept = np.polyfit(x, centered_y, 1, w=weights)
        expected_annualized = math.expm1(slope * 250)
        expected_residual = np.sum(
            importance * (centered_y - (slope * x + intercept)) ** 2
        )
        expected_total = np.sum(importance * centered_y ** 2)
        expected_r_squared = 1 - expected_residual / expected_total

        self.assertAlmostEqual(annualized, expected_annualized, places=14)
        self.assertAlmostEqual(r_squared, expected_r_squared, places=14)
        self.assertAlmostEqual(score, expected_annualized * expected_r_squared, places=14)

    def test_legacy_regression_preserves_v1_mixed_weight_formula(self) -> None:
        prices = [100.0, 101.5, 102.0, 104.2, 105.8, 108.1]
        annualized, r_squared, score = (
            SevenStarEtfRotationStrategy._legacy_weighted_trend(prices, 5)
        )

        y = np.log(np.asarray(prices))
        x = np.arange(len(y))
        weights = np.linspace(1, 2, len(y))
        slope, intercept = np.polyfit(x, y, 1, w=weights)
        expected_annualized = math.exp(slope * 250) - 1
        expected_residual = np.sum(weights * (y - (slope * x + intercept)) ** 2)
        expected_total = np.sum(weights * (y - np.mean(y)) ** 2)
        expected_r_squared = 1 - expected_residual / expected_total

        self.assertAlmostEqual(annualized, expected_annualized, places=14)
        self.assertAlmostEqual(r_squared, expected_r_squared, places=14)
        self.assertAlmostEqual(score, expected_annualized * expected_r_squared, places=14)

    def test_weighted_regression_handles_flat_and_invalid_prices(self) -> None:
        self.assertEqual(
            SevenStarEtfRotationStrategy._weighted_trend([50.03] * 26, 25),
            (0.0, 0.0, 0.0),
        )
        adjacent = float(np.nextafter(50.03, math.inf))
        near_flat = [50.03 if index % 2 == 0 else adjacent for index in range(26)]
        self.assertEqual(
            SevenStarEtfRotationStrategy._weighted_trend(near_flat, 25),
            (0.0, 0.0, 0.0),
        )
        micro_trend = [100 * math.exp(1e-8 * index) for index in range(26)]
        micro_annualized, micro_r_squared, _ = (
            SevenStarEtfRotationStrategy._weighted_trend(micro_trend, 25)
        )
        self.assertGreater(micro_annualized, 0)
        self.assertAlmostEqual(micro_r_squared, 1.0, places=12)
        for prices, lookback in (([1, 2], 5), ([1, 0, 2], 2), ([1, math.nan, 2], 2)):
            with self.subTest(prices=prices, lookback=lookback):
                with self.assertRaises(BacktestDataError):
                    SevenStarEtfRotationStrategy._weighted_trend(prices, lookback)

    def test_weighted_regression_r_squared_sign_and_scale_invariants(self) -> None:
        declining_rebound = [98, 100, 102, 102, 102, 98]
        annualized, r_squared, score = SevenStarEtfRotationStrategy._weighted_trend(
            declining_rebound, 5
        )
        self.assertLess(annualized, 0)
        self.assertGreaterEqual(r_squared, 0)
        self.assertLessEqual(r_squared, 1)
        self.assertLessEqual(score, 0)

        scaled = SevenStarEtfRotationStrategy._weighted_trend(
            [value * 1_000 for value in declining_rebound], 5
        )
        for actual, expected in zip(scaled, (annualized, r_squared, score)):
            self.assertAlmostEqual(actual, expected, places=12)

        rng = np.random.default_rng(20260801)
        for _ in range(100):
            prices = np.exp(np.cumsum(rng.normal(0, 0.02, 26))) * 100
            sample_annualized, value, sample_score = SevenStarEtfRotationStrategy._weighted_trend(
                prices.tolist(), 25
            )
            self.assertTrue(
                all(math.isfinite(item) for item in (sample_annualized, value, sample_score))
            )
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 1)
            self.assertGreaterEqual(sample_annualized * sample_score, 0)

        extreme = [math.exp(3 * index) for index in range(6)]
        extreme_result = SevenStarEtfRotationStrategy._weighted_trend(extreme, 5)
        self.assertTrue(all(math.isfinite(item) for item in extreme_result))
        self.assertEqual(extreme_result[0], float(np.finfo(float).max))
        self.assertGreater(extreme_result[2], 1000)

    def test_non_positive_long_trend_is_an_explicit_filter(self) -> None:
        trading_day = "2024-01-09"
        prices = [98, 100, 102, 102, 102, 98]
        history = rows(date(2024, 1, 2), 5, 100, 1)
        for row, close in zip(history, prices[:-1]):
            row.update(open=close, high=close, low=close, close=close)
        dataset = HistoricalDataSet(
            daily={"AAA": history},
            sessions=[trading_day],
            availability_start={"AAA": trading_day},
        )

        class Context:
            event = "14:00"
            trading_date = trading_day
            event_prices = {"AAA": EventPrice(prices[-1], prices[-1], "signal", "fill")}

        Context.dataset = dataset
        strategy = SevenStarEtfRotationStrategy(
            {
                "lookback_days": 5,
                "enable_profit_protection": False,
                "enable_volume_check": False,
                "enable_short_momentum_filter": False,
                "short_lookback_days": 2,
                "single_day_loss_percent": 30,
            }
        )
        with patch.object(
            SevenStarEtfRotationStrategy,
            "_weighted_trend",
            return_value=(-0.1, 0.5, 0.1),
        ):
            metrics = strategy._metrics(Context, "AAA")
        self.assertFalse(metrics["eligible"])
        self.assertIn("non_positive_trend", metrics["filter_codes"])
        self.assertIn("长期拟合趋势非正", metrics["filter_reasons"])
        self.assertLess(metrics["annualized_returns"], 0)
        self.assertGreaterEqual(metrics["r_squared"], 0)

        legacy_strategy = SevenStarEtfRotationStrategy(
            {
                "trend_formula_mode": "legacy_v1",
                "lookback_days": 5,
                "enable_profit_protection": False,
                "enable_volume_check": False,
                "enable_short_momentum_filter": False,
                "short_lookback_days": 2,
                "single_day_loss_percent": 30,
            }
        )
        legacy_metrics = legacy_strategy._metrics(Context, "AAA")
        self.assertEqual(legacy_metrics["trend_formula_mode"], "legacy_v1")
        self.assertLess(legacy_metrics["annualized_returns"], 0)
        self.assertLess(legacy_metrics["r_squared"], 0)
        self.assertGreater(legacy_metrics["score"], 0)
        self.assertTrue(legacy_metrics["eligible"])
        self.assertNotIn("non_positive_trend", legacy_metrics["filter_codes"])

    def test_parameter_cross_constraints_fail_closed(self) -> None:
        with self.assertRaisesRegex(Exception, "trend_formula_mode"):
            SevenStarEtfRotationStrategy({"trend_formula_mode": "unknown"})
        with self.assertRaisesRegex(Exception, "最低趋势得分"):
            SevenStarEtfRotationStrategy({
                "min_score_threshold": 1,
                "max_score_threshold": 1,
            })
        with self.assertRaisesRegex(Exception, "12:55"):
            SevenStarEtfRotationStrategy({"profit_check_time": "13:00"})
        definition = {
            "symbols": [{"symbol": "SPY", "max_weight": 100}],
            "params": SevenStarEtfRotationStrategy.validate_params({"holdings_num": 2}),
        }
        with self.assertRaisesRegex(Exception, "不能超过"):
            SevenStarEtfRotationStrategy.validate_definition(definition)

    def test_filter_boundaries_preserve_strict_original_comparisons(self) -> None:
        trading_day = "2024-02-09"
        history = rows(date(2024, 1, 2), 29, 100, 1.004)
        history = [row for row in history if row["date"] < trading_day][-25:]
        current = float(history[-1]["close"]) * 1.004
        dataset = HistoricalDataSet(
            daily={"AAA": [*history, {**history[-1], "date": trading_day}]},
            sessions=[trading_day],
            cumulative_volumes={
                "AAA": {f"{trading_day}|14:00": 2_000_000.0}
            },
            availability_start={"AAA": trading_day},
        )

        class Context:
            universe = ["AAA"]
            event = "14:00"
            trading_date = trading_day
            event_prices = {
                "AAA": EventPrice(current, current, "signal", "fill")
            }

        Context.dataset = dataset
        strategy = SevenStarEtfRotationStrategy(
            {
                "enable_profit_protection": False,
                "enable_short_momentum_filter": False,
                "volume_return_limit_percent": 0,
                "volume_ratio_threshold": 2,
                "max_score_threshold": 1000,
            }
        )
        # Exactly 2x does not pass the original strict `ratio > threshold` test.
        accepted = strategy._metrics(Context, "AAA")
        self.assertTrue(accepted["eligible"])
        dataset.cumulative_volumes["AAA"][f"{trading_day}|14:00"] = 2_000_001
        rejected = strategy._metrics(Context, "AAA")
        self.assertFalse(rejected["eligible"])
        self.assertIn("volume_overheat", rejected["filter_codes"])

        boundary = accepted["score"]
        no_volume = SevenStarEtfRotationStrategy(
            {
                "enable_profit_protection": False,
                "enable_volume_check": False,
                "enable_short_momentum_filter": False,
                "min_score_threshold": boundary,
                "max_score_threshold": boundary + 1,
            }
        )
        score_rejected = no_volume._metrics(Context, "AAA")
        self.assertFalse(score_rejected["eligible"])
        self.assertIn("score_range", score_rejected["filter_codes"])

    def test_profit_protection_triggers_on_equality(self) -> None:
        trading_day = "2024-02-09"
        history = rows(date(2024, 1, 2), 28, 100, 1.001)
        history = [row for row in history if row["date"] < trading_day][-25:]
        trigger_price = float(history[-1]["high"]) * 0.95
        dataset = HistoricalDataSet(
            daily={"AAA": [*history, {**history[-1], "date": trading_day}]},
            sessions=[trading_day], availability_start={"AAA": trading_day},
        )

        class Context:
            trading_date = trading_day
            event_prices = {
                "AAA": EventPrice(trigger_price, trigger_price, "signal", "fill")
            }

        Context.dataset = dataset
        self.assertTrue(SevenStarEtfRotationStrategy()._profit_triggered(Context, "AAA"))

    def test_minimum_trade_value_never_blocks_full_liquidation(self) -> None:
        portfolio = Portfolio(10_000, commission_per_share=0, minimum_commission=0)
        marks = {"SPY": 100}
        portfolio.execute(
            OrderIntent("SPY", "BUY", "TARGET", 50, "initial"),
            reference_price=100, marks=marks, event_time="buy",
        )
        skipped = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 40, "small", 2_000),
            reference_price=100, marks=marks, event_time="partial",
        )
        self.assertIsNone(skipped)
        liquidated = portfolio.execute(
            OrderIntent("SPY", "SELL", "TARGET", 0, "risk exit", 100_000),
            reference_price=100, marks=marks, event_time="sell all",
        )
        self.assertEqual(liquidated["quantity"], 50)
        self.assertEqual(float(portfolio.quantity("SPY")), 0)

    def test_btc_quantity_is_rounded_to_configured_step(self) -> None:
        portfolio = Portfolio(1_000, quantity_steps={"BTC/USD": 0.0001})
        trade = portfolio.execute(
            OrderIntent("BTC/USD", "BUY", "TARGET", 33.33, "btc"),
            reference_price=60_000, marks={"BTC/USD": 60_000},
            event_time="buy",
        )
        self.assertAlmostEqual(trade["quantity"] * 10_000, round(trade["quantity"] * 10_000))

    def test_engine_sells_before_buy_and_uses_next_minute_open(self) -> None:
        sessions = ["2024-02-08", "2024-02-09"]
        start = date(2024, 1, 2)
        daily = {
            "AAA": rows(start, 29, 50, 1.01),
            "BBB": rows(start, 29, 50, 1.001),
        }
        for symbol in daily:
            # Force the two session bars to match the explicit minute marks.
            daily[symbol][-2]["date"] = sessions[0]
            daily[symbol][-1]["date"] = sessions[1]
        minute = {symbol: {} for symbol in daily}
        resolutions = {symbol: {} for symbol in daily}
        prices = {
            sessions[0]: {"AAA": (80, 80.1), "BBB": (53, 53.1)},
            sessions[1]: {"AAA": (40, 40.1), "BBB": (65, 65.1)},
        }
        for trading_day in sessions:
            for symbol in daily:
                sell_target = epoch_minute(trading_day, "14:00")
                buy_target = epoch_minute(trading_day, "14:01")
                sell_price, buy_price = prices[trading_day][symbol]
                minute[symbol].update(
                    {
                        sell_target - 1: {"close": sell_price},
                        sell_target: {"open": sell_price + 0.01, "close": buy_price},
                        buy_target: {"open": buy_price + 0.01},
                    }
                )
                resolutions[symbol][f"{trading_day}|14:00"] = {
                    "signal_minute": sell_target - 1, "fill_minute": sell_target,
                }
                resolutions[symbol][f"{trading_day}|14:01"] = {
                    "signal_minute": sell_target, "fill_minute": buy_target,
                }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=sessions,
            minute=minute,
            intraday_event_minutes=resolutions,
            availability_start={symbol: sessions[0] for symbol in daily},
            manifest={"symbols": {}},
        )
        params = SevenStarEtfRotationStrategy.validate_params(
            {
                "enable_profit_protection": False,
                "enable_volume_check": False,
                "enable_short_momentum_filter": False,
                "single_day_loss_percent": 30,
                "max_score_threshold": 1000,
            }
        )
        strategy = {
            "name": "七星事件顺序测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": SevenStarEtfRotationStrategy.key,
            "code_version": SevenStarEtfRotationStrategy.version,
            "definition": {
                "symbols": [
                    {"symbol": "AAA", "max_weight": 100},
                    {"symbol": "BBB", "max_weight": 100},
                ],
                "params": params,
            },
            "default_settings": {},
        }
        settings = {
            "start_date": sessions[0], "end_date": sessions[-1],
            "initial_capital": 100_000, "commission_per_share": 0.01,
            "minimum_commission": 1, "slippage_bps": 0,
            "allow_fractional_shares": False, "benchmark": "none",
            "risk_free_rate": 0, "strict_data": True,
        }
        result = BacktestEngine(strategy, settings, dataset=dataset).run()

        self.assertEqual(
            [trade["side"] for trade in result.trades], ["BUY", "SELL", "BUY"],
            msg={"trades": result.trades, "logs": result.logs},
        )
        self.assertEqual([trade["symbol"] for trade in result.trades], ["AAA", "AAA", "BBB"])
        self.assertIn("14:01", result.trades[0]["event_time"])
        self.assertIn("14:00", result.trades[1]["event_time"])
        self.assertIn("14:01", result.trades[2]["event_time"])
        self.assertAlmostEqual(result.trades[2]["reference_price"], 65.11)
        self.assertAlmostEqual(
            result.metrics["ending_equity"], result.equity_points[-1]["equity"]
        )
        score_logs = [
            log for log in result.logs
            if log["event_type"] == "SEVENSTAR_DAILY_SCORE"
        ]
        self.assertEqual(len(score_logs), len(sessions) * len(daily))
        self.assertTrue(all(log["level"] == "DEBUG" for log in score_logs))
        self.assertTrue(all(log["symbol"] in daily for log in score_logs))
        self.assertTrue(all("score" in log["context"] for log in score_logs))
        self.assertTrue(all("filter_codes" in log["context"] for log in score_logs))


class SevenStarDataContractTests(unittest.TestCase):
    @patch("services.backtest.data.ensure_corporate_actions", return_value=[])
    @patch("services.backtest.data.repository.get_symbol")
    @patch("services.backtest.data.repository.get_daily_prices")
    @patch("services.backtest.data.ensure_market_sessions")
    def test_missing_optional_defensive_symbol_keeps_strategy_in_cash_capable(
        self, get_sessions, get_daily, get_symbol, actions
    ) -> None:
        trading_day = "2024-01-03"
        get_sessions.return_value = [{
            "trading_date": trading_day,
            "open_minute_utc": epoch_minute(trading_day, "09:30"),
            "close_minute_utc": epoch_minute(trading_day, "16:00"),
            "is_early_close": False,
        }]
        get_daily.side_effect = lambda symbol, **_: (
            [
                {
                    "date": "2024-01-02", "open": 100, "high": 101,
                    "low": 99, "close": 100, "volume": 1000,
                    "is_complete": 1,
                },
                {
                    "date": trading_day, "open": 100, "high": 101,
                    "low": 99, "close": 100, "volume": 1000,
                    "is_complete": 1,
                },
            ]
            if symbol == "SPY"
            else []
        )
        get_symbol.return_value = {
            "asset_class": "us_equity", "quantity_step": None,
            "history_start_date": None,
        }

        dataset = load_historical_dataset(
            universe=["SPY", "BIL"], additional_symbols=[],
            optional_symbols=["BIL"], start_date=trading_day,
            end_date=trading_day, intraday_events=[], minimum_lookback=1,
        )

        self.assertIsNone(dataset.availability_start["BIL"])
        self.assertFalse(dataset.is_eligible("BIL", trading_day))
        self.assertEqual(actions.call_args.args[0], ["SPY"])

    @patch("services.backtest.data.repository.get_daily_prices", return_value=[])
    def test_missing_required_symbol_error_names_symbol_and_date(
        self, _get_daily
    ) -> None:
        with self.assertRaises(Exception) as captured:
            load_historical_dataset(
                universe=["DIA"], additional_symbols=[],
                start_date="2024-01-02", end_date="2024-12-31",
                intraday_events=[], minimum_lookback=1,
            )
        self.assertIn("DIA", str(captured.exception))
        self.assertIn("2024-01-02", str(captured.exception))
        self.assertEqual(captured.exception.detail["symbol"], "DIA")
        self.assertEqual(captured.exception.detail["missing_date"], "2024-01-02")

    @patch("services.backtest.data.ensure_corporate_actions", return_value=[])
    @patch("services.backtest.data.repository.get_symbol")
    @patch("services.backtest.data.repository.get_daily_prices")
    @patch("services.backtest.data.ensure_market_sessions")
    def test_late_inception_is_marked_and_warmed_up_dynamically(
        self, get_sessions, get_daily, get_symbol, _actions
    ) -> None:
        sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
        get_sessions.return_value = [
            {
                "trading_date": value,
                "open_minute_utc": epoch_minute(value, "09:30"),
                "close_minute_utc": epoch_minute(value, "16:00"),
                "is_early_close": False,
            }
            for value in sessions
        ]
        spy = rows(date(2023, 12, 28), 6, 100, 1.001)
        mags = rows(date(2024, 1, 3), 2, 30, 1.001)
        get_daily.side_effect = lambda symbol, **_: spy if symbol == "SPY" else mags
        get_symbol.side_effect = lambda symbol: {
            "asset_class": "us_equity", "quantity_step": None,
            "history_start_date": "2024-01-03" if symbol == "MAGS" else "2023-12-28",
        }

        dataset = load_historical_dataset(
            universe=["SPY", "MAGS"], additional_symbols=[],
            start_date=sessions[0], end_date=sessions[-1],
            intraday_events=[], minimum_lookback=1,
        )

        self.assertEqual(dataset.availability_start["MAGS"], "2024-01-04")
        self.assertFalse(dataset.is_eligible("MAGS", "2024-01-03"))
        self.assertTrue(dataset.is_eligible("MAGS", "2024-01-04"))
        self.assertEqual(
            dataset.manifest["symbols"]["MAGS"]["history_start_date"],
            "2024-01-03",
        )

    @patch("services.backtest.data.intraday_repository.resolve_minute_event_gaps", return_value={})
    @patch("services.backtest.data.intraday_repository.get_minute_bars_at")
    @patch("services.backtest.data.ensure_corporate_actions", return_value=[])
    @patch("services.backtest.data.repository.get_symbol")
    @patch("services.backtest.data.repository.get_daily_prices")
    @patch("services.backtest.data.ensure_market_sessions")
    def test_early_close_maps_sell_and_buy_to_last_two_minutes(
        self, get_sessions, get_daily, get_symbol, _actions, get_minutes, _gaps
    ) -> None:
        trading_day = "2024-11-29"
        get_sessions.return_value = [{
            "trading_date": trading_day,
            "open_minute_utc": epoch_minute(trading_day, "09:30"),
            "close_minute_utc": epoch_minute(trading_day, "13:00"),
            "is_early_close": True,
        }]
        get_daily.return_value = [
            {
                "date": "2024-11-27", "open": 100, "high": 101, "low": 99,
                "close": 100, "volume": 1000, "is_complete": 1,
            },
            {
                "date": trading_day, "open": 100, "high": 101, "low": 99,
                "close": 100, "volume": 1000, "is_complete": 1,
            },
        ]
        get_symbol.return_value = {
            "asset_class": "us_equity", "quantity_step": None,
            "history_start_date": "2024-11-27",
        }

        def minute_rows(_symbol, requested):
            return {
                value: {
                    "minute_utc": value, "open": 100, "high": 100, "low": 100,
                    "close": 100, "volume": 10,
                }
                for value in requested
            }

        get_minutes.side_effect = minute_rows
        dataset = load_historical_dataset(
            universe=["SPY"], additional_symbols=[], start_date=trading_day,
            end_date=trading_day, intraday_events=["14:00", "14:01"],
            minimum_lookback=1, early_close_offsets={"14:00": 2, "14:01": 1},
        )

        sell = dataset.event_price("SPY", trading_day, "14:00")
        buy = dataset.event_price("SPY", trading_day, "14:01")
        self.assertIn("12:58", sell.fill_time)
        self.assertIn("12:59", buy.fill_time)


if __name__ == "__main__":
    unittest.main()
