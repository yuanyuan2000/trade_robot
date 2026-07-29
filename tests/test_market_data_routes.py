from __future__ import annotations

import unittest
from unittest.mock import patch

import app as app_module


class MarketDataRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()

    @patch.object(app_module, "get_market_data")
    def test_query_can_request_intraday_initialization(self, get_market_data) -> None:
        get_market_data.return_value = {"ok": True, "data": []}

        response = self.client.get(
            "/api/market-data?symbol=GLD&include_intraday=1"
        )

        self.assertEqual(response.status_code, 200)
        get_market_data.assert_called_once_with(
            "GLD",
            include_intraday=True,
        )

    @patch.object(app_module, "update_full_market_data")
    def test_regular_update_without_checkbox_only_updates_daily(self, update) -> None:
        update.return_value = {"ok": True, "data": []}

        response = self.client.post(
            "/api/market-data/update",
            json={"symbol": "GLD"},
        )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(
            "GLD",
            initialize_intraday=False,
        )

    @patch.object(app_module, "update_full_market_data")
    def test_regular_update_with_checkbox_updates_intraday(self, update) -> None:
        update.return_value = {"ok": True, "data": []}

        response = self.client.post(
            "/api/market-data/update",
            json={"symbol": "GLD", "include_intraday": True},
        )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(
            "GLD",
            initialize_intraday=True,
        )


if __name__ == "__main__":
    unittest.main()
