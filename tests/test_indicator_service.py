from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import database.db as main_db
from database import repository
from services.backtest.data import HistoricalDataSet
from services.backtest.code_strategies import SevenStarEtfRotationStrategy
from services.indicator_service import (
    attach_overview_indicator_values,
    build_indicator_series,
    calculate_indicator_values,
    calculate_r_square,
    calculate_macd_components,
    calculate_wilder_rsi,
    calculate_rapid_drop_filter,
    calculate_wilder_atr,
    calculate_wtme,
    calculate_wtme_components,
)


def sample_rows() -> list[dict]:
    return [
        {"date": "2024-01-01", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
        {"date": "2024-01-02", "open": 100, "high": 101, "low": 100, "close": 101, "volume": 1},
        {"date": "2024-01-03", "open": 101, "high": 103, "low": 101, "close": 103, "volume": 1},
        {"date": "2024-01-04", "open": 103, "high": 106, "low": 103, "close": 106, "volume": 1},
        {"date": "2024-01-05", "open": 106, "high": 110, "low": 106, "close": 110, "volume": 1},
    ]


def simple_rows(closes: list[float], scale: float = 1.0) -> list[dict]:
    return [
        {
            "date": f"2024-02-{index + 1:02d}",
            "open": close * scale,
            "high": close * scale,
            "low": close * scale,
            "close": close * scale,
            "volume": 1,
        }
        for index, close in enumerate(closes)
    ]


class IndicatorCalculationTests(unittest.TestCase):
    def test_rsi_and_macd_catalog_parameters_use_standard_defaults(self) -> None:
        self.assertEqual(repository.validate_indicator("RSI", {"period": 14}), ("RSI", {"period": 14}))
        self.assertEqual(
            repository.validate_indicator("MACD", {}),
            ("MACD", {"fast_period": 12, "slow_period": 26, "signal_period": 9}),
        )
        with self.assertRaisesRegex(ValueError, "快线周期必须小于慢线周期"):
            repository.validate_indicator("MACD", {"fast_period": 26, "slow_period": 12, "signal_period": 9})

    def test_wilder_rsi_handles_rising_falling_and_flat_prices(self) -> None:
        rising = simple_rows([100, 101, 102, 103, 104])
        falling = simple_rows([100, 99, 98, 97, 96])
        flat = simple_rows([100, 100, 100, 100, 100])
        self.assertEqual(calculate_wilder_rsi(rising, 3)[-1], 100.0)
        self.assertEqual(calculate_wilder_rsi(falling, 3)[-1], 0.0)
        self.assertEqual(calculate_wilder_rsi(flat, 3)[-1], 50.0)

    def test_macd_returns_dif_dea_and_undoubled_histogram(self) -> None:
        rows = simple_rows([100 + index + (index % 3) for index in range(20)])
        components = calculate_macd_components(rows, 3, 6, 4)
        index = next(
            value for value in range(len(rows) - 1, -1, -1)
            if components["histogram"][value] is not None
        )
        self.assertAlmostEqual(
            components["histogram"][index],
            components["line"][index] - components["signal"][index],
        )
        series = build_indicator_series(
            rows,
            [{
                "id": 99,
                "indicator_type": "MACD",
                "params": {"fast_period": 3, "slow_period": 6, "signal_period": 4},
            }],
            price_basis="raw",
        )[0]
        point = series["points"][index]
        self.assertEqual(set(point["components"]), {"line", "signal", "histogram"})
        self.assertAlmostEqual(point["value"], point["components"]["histogram"])

    def test_indicator_catalog_accepts_configurable_atr_periods(self) -> None:
        self.assertEqual(repository.validate_indicator("ATR", {"period": 14}), ("ATR", {"period": 14}))
        self.assertEqual(repository.validate_indicator("ratr", {"period": "21"}), ("RATR", {"period": 21}))
        self.assertEqual(
            repository.validate_indicator("LINEAR_FIT", {"period": "25"}),
            ("LINEAR_FIT", {"period": 25}),
        )
        self.assertEqual(
            repository.validate_indicator(
                "wtme",
                {"period": "40", "half_life": "15", "epsilon": "1e-8"},
            ),
            ("WTME", {"period": 40, "half_life": 15.0, "epsilon": 1e-8}),
        )
        self.assertEqual(
            repository.validate_indicator(
                "rapid_drop",
                {"period": "5", "threshold_percent": "5"},
            ),
            ("RAPID_DROP", {"period": 5, "threshold_percent": 5.0}),
        )

    def test_rapid_drop_indicator_validates_strategy_compatible_ranges(self) -> None:
        self.assertEqual(
            repository.validate_indicator(
                "RAPID_DROP", {"period": 1, "threshold_percent": 0.1}
            ),
            ("RAPID_DROP", {"period": 1, "threshold_percent": 0.1}),
        )
        with self.assertRaisesRegex(ValueError, "0.1% 到 50%"):
            repository.validate_indicator(
                "RAPID_DROP", {"period": 5, "threshold_percent": 0}
            )

    def test_wtme_matches_weighted_formula(self) -> None:
        rows = sample_rows()[:4]
        components = calculate_wtme_components(rows, 3, 2, 1e-8)

        self.assertIsNotNone(components)
        raw_weights = [0.5, 2 ** -0.5, 1.0]
        total = sum(raw_weights)
        weights = [value / total for value in raw_weights]
        returns = [0.01, 2 / 101, 3 / 103]
        true_ranges = [0.01, 2 / 101, 3 / 103]
        expected_return = sum(w * value for w, value in zip(weights, returns))
        expected_range = sum(w * value for w, value in zip(weights, true_ranges))
        self.assertAlmostEqual(components["weighted_return"], expected_return)
        self.assertAlmostEqual(components["weighted_true_range"], expected_range)
        self.assertAlmostEqual(
            components["value"],
            100 * expected_return / (expected_range + 1e-8),
        )
        self.assertAlmostEqual(sum(components["weights"]), 1.0)
        self.assertAlmostEqual(components["weights"][0], 0.5 / total)
        self.assertAlmostEqual(components["weights"][-1], 1.0 / total)

    def test_wtme_reflects_direction_efficiency_and_price_scale_invariance(self) -> None:
        def rows_for(closes: list[float], scale: float = 1.0) -> list[dict]:
            result = []
            for index, close in enumerate(closes):
                previous = closes[index - 1] if index else close
                result.append({
                    "date": f"2024-01-{index + 1:02d}",
                    "open": previous * scale,
                    "high": max(previous, close) * scale,
                    "low": min(previous, close) * scale,
                    "close": close * scale,
                    "volume": 1,
                })
            return result

        smooth = rows_for([100, 101, 102, 103, 104])
        oscillating = rows_for([100, 110, 100, 110, 104])
        falling = rows_for([104, 103, 102, 101, 100])
        smooth_score = calculate_wtme(smooth, 4, 1000)[-1]
        oscillating_score = calculate_wtme(oscillating, 4, 1000)[-1]
        falling_score = calculate_wtme(falling, 4, 1000)[-1]
        scaled_score = calculate_wtme(rows_for([100, 101, 102, 103, 104], 37), 4, 1000)[-1]

        self.assertGreater(smooth_score, 99.9)
        self.assertLess(abs(oscillating_score), 30)
        self.assertLess(falling_score, -99.9)
        self.assertAlmostEqual(smooth_score, scaled_score, places=10)
    def test_wilder_atr_matches_strategy_recurrence(self) -> None:
        values = calculate_wilder_atr(sample_rows(), 3)

        self.assertEqual(values[:3], [None, None, None])
        self.assertAlmostEqual(values[3], 2.0)
        self.assertAlmostEqual(values[4], 8 / 3)

    def test_relative_atr_uses_prior_completed_atr_without_lookahead(self) -> None:
        values = calculate_indicator_values(sample_rows(), "RATR", 3)

        self.assertEqual(values[:4], [None, None, None, None])
        self.assertAlmostEqual(values[4], (110 - 101) / 2)

    def test_r_square_matches_sevenstar_consistent_r_squared(self) -> None:
        prices = [100.0, 101.5, 102.0, 104.2, 105.8, 108.1]
        values = calculate_r_square(simple_rows(prices), 5)
        _, expected_r_squared, _ = SevenStarEtfRotationStrategy._weighted_trend(
            prices, 5
        )

        self.assertEqual(values[:5], [None] * 5)
        self.assertAlmostEqual(values[-1], expected_r_squared, places=14)

    def test_r_square_handles_flat_scale_and_future_rows(self) -> None:
        flat = calculate_r_square(simple_rows([50.03] * 6), 5)
        smooth_prices = [100 * (1.01 ** index) for index in range(8)]
        smooth = calculate_r_square(simple_rows(smooth_prices), 5)
        scaled = calculate_r_square(
            simple_rows(smooth_prices, scale=37), 5
        )
        extended = calculate_r_square(
            simple_rows([*smooth_prices, 1_000_000]), 5
        )

        self.assertEqual(flat[-1], 0.0)
        self.assertAlmostEqual(smooth[-1], 1.0, places=12)
        for actual, expected in zip(scaled, smooth):
            if expected is not None:
                self.assertAlmostEqual(actual, expected, places=12)
        self.assertEqual(extended[:len(smooth)], smooth)

    def test_visual_expression_context_reuses_indicator_system_values(self) -> None:
        rows = sample_rows()
        trading_date = rows[-1]["date"]
        dataset = HistoricalDataSet(
            daily={"SPY": rows},
            sessions=[trading_date],
        )
        context = dataset.expression_context(
            symbol="SPY",
            trading_date=trading_date,
            event="CLOSE",
            price=float(rows[-1]["close"]),
            position=0,
        )
        cases = (
            ("ma", (3,), "MA", {"period": 3}),
            ("ema", (3,), "EMA", {"period": 3}),
            ("atr", (3,), "ATR", {"period": 3}),
            ("ratr", (3,), "RATR", {"period": 3}),
            ("r_square", (3,), "LINEAR_FIT", {"period": 3}),
            ("rsi", (3,), "RSI", {"period": 3}),
            (
                "wtme",
                (3, 2, 1e-8),
                "WTME",
                {"period": 3, "half_life": 2, "epsilon": 1e-8},
            ),
            (
                "rapid_drop",
                (2, 5),
                "RAPID_DROP",
                {"period": 2, "threshold_percent": 5},
            ),
        )
        for function_name, arguments, indicator_type, params in cases:
            with self.subTest(function=function_name):
                expected = calculate_indicator_values(
                    rows,
                    indicator_type,
                    params["period"],
                    half_life=params.get("half_life"),
                    epsilon=params.get("epsilon", 1e-8),
                    threshold_percent=params.get("threshold_percent"),
                )[-1]
                self.assertIsNotNone(expected)
                self.assertAlmostEqual(
                    context.resolve_function(function_name, *arguments),
                    expected,
                    places=12,
                )
        macd = calculate_macd_components(rows, 2, 3, 2)
        self.assertAlmostEqual(context.resolve_function("macd_line", 2, 3), macd["line"][-1])
        self.assertAlmostEqual(context.resolve_function("macd_signal", 2, 3, 2), macd["signal"][-1])
        self.assertAlmostEqual(context.resolve_function("macd_hist", 2, 3, 2), macd["histogram"][-1])

    def test_rapid_drop_filter_checks_n_changes_and_includes_latest_bar(self) -> None:
        rows = [
            {"date": "2024-01-01", "close": 100, "is_complete": 1},
            {"date": "2024-01-02", "close": 98, "is_complete": 1},
            {"date": "2024-01-03", "close": 93.1, "is_complete": 1},
            {"date": "2024-01-04", "close": 94, "is_complete": 1},
            {"date": "2024-01-05", "close": 89.3, "is_complete": 0},
        ]

        values = calculate_rapid_drop_filter(rows, 2, 5)

        self.assertEqual(values[:2], [None, None])
        self.assertEqual(values[2], 1.0)  # exactly -5% triggers, matching the strategies
        self.assertEqual(values[3], 1.0)  # the hit remains in the two-change window
        self.assertEqual(values[4], 1.0)  # unfinished latest bar is included

    def test_rapid_drop_filter_returns_zero_when_window_has_no_hit(self) -> None:
        rows = [
            {"date": "2024-01-01", "close": 100},
            {"date": "2024-01-02", "close": 96},
            {"date": "2024-01-03", "close": 94},
        ]

        self.assertEqual(calculate_rapid_drop_filter(rows, 2, 5)[-1], 0.0)

    def test_overview_attaches_latest_value_and_data_date(self) -> None:
        overview = {"items": [{"symbol": "SPY"}]}
        indicator = {
            "id": 7,
            "name": "相对ATR3",
            "indicator_type": "RATR",
            "params": {"period": 3},
        }

        result = attach_overview_indicator_values(
            overview,
            [indicator],
            {"SPY": sample_rows()},
        )

        reading = result["items"][0]["indicator_values"]["7"]
        self.assertAlmostEqual(reading["value"], 4.5)
        self.assertEqual(reading["date"], "2024-01-05")

    def test_overview_rapid_drop_value_uses_unfinished_latest_daily_bar(self) -> None:
        rows = sample_rows()
        rows[-1] = {**rows[-1], "close": 100, "is_complete": 0}
        overview = {"items": [{"symbol": "SPY"}]}
        indicator = {
            "id": 8,
            "name": "急跌过滤2日5%",
            "indicator_type": "RAPID_DROP",
            "params": {"period": 2, "threshold_percent": 5},
        }

        result = attach_overview_indicator_values(
            overview,
            [indicator],
            {"SPY": rows},
        )

        reading = result["items"][0]["indicator_values"]["8"]
        self.assertEqual(reading["value"], 1.0)
        self.assertEqual(reading["date"], "2024-01-05")
        self.assertTrue(reading["is_provisional"])
        self.assertEqual(reading["price_basis"], "raw")
        self.assertEqual(reading["indicator_contract_version"], 1)

    def test_chart_series_and_overview_use_the_same_backend_result(self) -> None:
        rows = sample_rows()
        rows[-1] = {
            **rows[-1],
            "is_complete": 0,
            "updated_at": "2024-01-05T15:00:00-05:00",
        }
        indicators = [
            {"id": 1, "indicator_type": "MA", "params": {"period": 3}},
            {"id": 2, "indicator_type": "EMA", "params": {"period": 3}},
            {"id": 3, "indicator_type": "ATR", "params": {"period": 3}},
            {"id": 4, "indicator_type": "RATR", "params": {"period": 3}},
            {"id": 5, "indicator_type": "LINEAR_FIT", "params": {"period": 3}},
            {"id": 6, "indicator_type": "WTME", "params": {"period": 3, "half_life": 2, "epsilon": 1e-8}},
        ]
        overview = attach_overview_indicator_values(
            {"items": [{"symbol": "SPY"}]},
            indicators,
            {"SPY": rows},
            {"SPY": {"price_basis": "all_adjusted"}},
        )
        series = build_indicator_series(
            rows,
            indicators,
            price_basis="all_adjusted",
        )

        overview_values = overview["items"][0]["indicator_values"]
        for item in series:
            with self.subTest(indicator=item["indicator_type"]):
                self.assertEqual(item["points"][-1]["value"], overview_values[str(item["id"])]["value"])
                self.assertEqual(item["price_basis"], "all_adjusted")
                self.assertTrue(item["is_provisional"])
                self.assertEqual(item["indicator_contract_version"], 1)


class IndicatorSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.patcher.start()
        main_db.init_database()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_reseeding_defaults_preserves_user_favorite_choice(self) -> None:
        relative_atr = next(
            item for item in repository.list_indicators()
            if item["code"] == "RATR14"
        )
        self.assertTrue(relative_atr["is_favorite"])
        repository.update_indicator(
            relative_atr["id"],
            {"is_favorite": False},
        )

        repository.seed_default_indicators()

        stored = repository.get_indicator(relative_atr["id"])
        self.assertFalse(stored["is_favorite"])

    def test_default_rapid_drop_indicator_is_favorite(self) -> None:
        indicator = next(
            item for item in repository.list_indicators()
            if item["code"] == "RAPID_DROP5P5"
        )

        self.assertTrue(indicator["is_favorite"])
        self.assertEqual(
            indicator["params"],
            {"period": 5, "threshold_percent": 5.0},
        )

    def test_default_r_square_indicator_uses_25_intervals(self) -> None:
        indicator = next(
            item for item in repository.list_indicators()
            if item["code"] == "LINEAR_FIT25"
        )

        self.assertTrue(indicator["is_favorite"])
        self.assertEqual(indicator["name"], "R²")
        self.assertEqual(indicator["params"], {"period": 25})

    def test_reseeding_renames_existing_r_square_default(self) -> None:
        indicator = next(
            item for item in repository.list_indicators()
            if item["code"] == "LINEAR_FIT25"
        )
        repository.update_indicator(indicator["id"], {"name": "直线拟合度25"})

        repository.seed_default_indicators()

        self.assertEqual(repository.get_indicator(indicator["id"])["name"], "R²")


class MarketOverviewIndicatorRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()

    @patch.object(app_module, "stored_adjusted_daily_payload")
    @patch.object(app_module.repository, "get_symbol")
    @patch.object(app_module.repository, "get_daily_prices")
    @patch.object(app_module.repository, "list_market_overview")
    @patch.object(app_module.repository, "list_indicators")
    def test_route_returns_requested_favorite_indicator_values(
        self,
        list_indicators,
        list_overview,
        get_daily_prices,
        get_symbol,
        stored_adjusted,
    ) -> None:
        list_indicators.return_value = [
            {"id": 1, "name": "ATR3", "indicator_type": "ATR", "params": {"period": 3}, "is_favorite": True},
            {"id": 2, "name": "相对ATR3", "indicator_type": "RATR", "params": {"period": 3}, "is_favorite": True},
        ]
        list_overview.return_value = {
            "items": [{"symbol": "SPY"}],
            "page": 1,
            "page_size": 1,
            "total_rows": 1,
            "total_pages": 1,
        }
        get_daily_prices.return_value = sample_rows()
        get_symbol.return_value = {"asset_class": "us_equity"}
        stored_adjusted.return_value = {
            "rows": sample_rows(),
            "actions": [],
            "adjustment": "all",
            "warning": None,
            "action_source": "stored_only",
        }

        response = self.client.get("/api/market-overview?indicator_ids=2,1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["selected_indicators"]], [2, 1])
        self.assertAlmostEqual(payload["items"][0]["indicator_values"]["2"]["value"], 4.5)
        self.assertAlmostEqual(payload["items"][0]["indicator_values"]["1"]["value"], 8 / 3)
        self.assertEqual(payload["indicator_standard_price_basis"], "all_adjusted")
        self.assertEqual(payload["indicator_action_source"], "stored_only")
        self.assertEqual(payload["items"][0]["indicator_values"]["1"]["price_basis"], "all_adjusted")
        get_daily_prices.assert_called_once_with("SPY", include_metadata=True)
        stored_adjusted.assert_called_once_with(
            "SPY", get_daily_prices.return_value, get_symbol.return_value, mode="all"
        )

    @patch.object(app_module, "get_chart_bars")
    @patch.object(app_module.repository, "list_symbol_indicators")
    def test_detail_indicator_route_returns_server_series_for_selected_basis(
        self,
        list_symbol_indicators,
        get_chart_bars,
    ) -> None:
        indicators = [
            {"id": 7, "name": "WTME3", "indicator_type": "WTME", "params": {"period": 3, "half_life": 2, "epsilon": 1e-8}},
        ]
        rows = sample_rows()
        rows[-1] = {**rows[-1], "is_complete": 0, "updated_at": "2024-01-05T15:00:00-05:00"}
        list_symbol_indicators.return_value = indicators
        get_chart_bars.return_value = {
            "data": rows,
            "adjustment": "all",
            "source": "database",
            "period": "1D",
            "symbol_settings": {"show_weekend_data": True},
        }

        response = self.client.get(
            "/api/symbols/SPY/chart-views/1D/indicators?with_values=1&adjustment=all"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        expected = calculate_wtme(rows, 3, 2, 1e-8)[-1]
        self.assertEqual(payload["bars"], rows)
        self.assertAlmostEqual(payload["indicators"][0]["points"][-1]["value"], expected)
        self.assertEqual(payload["calculation"]["price_basis"], "all_adjusted")
        self.assertTrue(payload["calculation"]["is_provisional"])
        get_chart_bars.assert_called_once_with("SPY", "1D", 2000, "all")

    def test_browser_only_aligns_server_calculated_indicator_points(self) -> None:
        chart_script = (
            Path(__file__).parents[1] / "static" / "js" / "chart.js"
        ).read_text(encoding="utf-8")

        self.assertIn("alignIndicatorPoints", chart_script)
        self.assertNotIn("function calculateWTME", chart_script)
        self.assertNotIn("function calculateMA", chart_script)
        self.assertNotIn("function calculateEMA", chart_script)
        self.assertNotIn("function calculateWilderATR", chart_script)

    @patch.object(app_module.repository, "list_market_overview")
    @patch.object(app_module.repository, "list_indicators")
    def test_route_accepts_three_columns(self, list_indicators, list_overview) -> None:
        list_indicators.return_value = [
            {"id": index, "name": f"MA{index}", "indicator_type": "MA", "params": {"period": index + 1}, "is_favorite": True}
            for index in range(1, 4)
        ]
        list_overview.return_value = {"items": [], "page": 1, "page_size": 100, "total_rows": 0, "total_pages": 1}

        response = self.client.get("/api/market-overview?indicator_ids=1,2,3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["selected_indicators"]), 3)

    def test_route_rejects_more_than_three_columns(self) -> None:
        response = self.client.get("/api/market-overview?indicator_ids=1,2,3,4")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_INDICATOR")

    def test_market_page_moves_overview_indicator_controls_into_table_header(self) -> None:
        html = self.client.get("/").get_data(as_text=True)

        self.assertNotIn('id="overview-indicator-controls"', html)
        self.assertIn('<option value="ATR">', html)
        self.assertIn('<option value="RATR">', html)
        self.assertIn('<option value="LINEAR_FIT">', html)
        self.assertIn('<option value="WTME">', html)
        self.assertIn('<option value="RAPID_DROP">', html)
        self.assertIn('<option value="RSI">', html)
        self.assertIn('<option value="MACD">', html)
        self.assertIn('id="custom-indicator-half-life"', html)
        self.assertIn('id="custom-indicator-fast-period"', html)
        self.assertIn('id="custom-indicator-threshold"', html)


if __name__ == "__main__":
    unittest.main()
