from __future__ import annotations

import unittest
from unittest.mock import patch

import app as app_module
from database import repository
from services.indicator_service import (
    attach_overview_indicator_values,
    calculate_indicator_values,
    calculate_wilder_atr,
)


def sample_rows() -> list[dict]:
    return [
        {"date": "2024-01-01", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
        {"date": "2024-01-02", "open": 100, "high": 101, "low": 100, "close": 101, "volume": 1},
        {"date": "2024-01-03", "open": 101, "high": 103, "low": 101, "close": 103, "volume": 1},
        {"date": "2024-01-04", "open": 103, "high": 106, "low": 103, "close": 106, "volume": 1},
        {"date": "2024-01-05", "open": 106, "high": 110, "low": 106, "close": 110, "volume": 1},
    ]


class IndicatorCalculationTests(unittest.TestCase):
    def test_indicator_catalog_accepts_configurable_atr_periods(self) -> None:
        self.assertEqual(repository.validate_indicator("ATR", {"period": 14}), ("ATR", {"period": 14}))
        self.assertEqual(repository.validate_indicator("ratr", {"period": "21"}), ("RATR", {"period": 21}))

    def test_wilder_atr_matches_strategy_recurrence(self) -> None:
        values = calculate_wilder_atr(sample_rows(), 3)

        self.assertEqual(values[:3], [None, None, None])
        self.assertAlmostEqual(values[3], 2.0)
        self.assertAlmostEqual(values[4], 8 / 3)

    def test_relative_atr_uses_prior_completed_atr_without_lookahead(self) -> None:
        values = calculate_indicator_values(sample_rows(), "RATR", 3)

        self.assertEqual(values[:4], [None, None, None, None])
        self.assertAlmostEqual(values[4], (110 - 101) / 2)

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


class MarketOverviewIndicatorRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()

    @patch.object(app_module.repository, "get_daily_prices")
    @patch.object(app_module.repository, "list_market_overview")
    @patch.object(app_module.repository, "list_indicators")
    def test_route_returns_two_requested_favorite_indicator_values(
        self,
        list_indicators,
        list_overview,
        get_daily_prices,
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

        response = self.client.get("/api/market-overview?indicator_ids=2,1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["selected_indicators"]], [2, 1])
        self.assertAlmostEqual(payload["items"][0]["indicator_values"]["2"]["value"], 4.5)
        self.assertAlmostEqual(payload["items"][0]["indicator_values"]["1"]["value"], 8 / 3)

    def test_route_rejects_more_than_two_columns(self) -> None:
        response = self.client.get("/api/market-overview?indicator_ids=1,2,3")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_INDICATOR")

    def test_market_page_exposes_atr_types_and_two_overview_columns(self) -> None:
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="overview-indicator-1"', html)
        self.assertIn('id="overview-indicator-2"', html)
        self.assertIn('<option value="ATR">', html)
        self.assertIn('<option value="RATR">', html)


if __name__ == "__main__":
    unittest.main()
