from __future__ import annotations

import unittest
from unittest.mock import patch

import app as app_module


class ManualAnalysisRefreshTests(unittest.TestCase):
    @patch.object(app_module, "start_analysis_overview_refresh")
    @patch.object(app_module, "sync_market_overview_daily_prices")
    def test_market_startup_sync_does_not_trigger_trendline_refresh(
        self,
        sync_market,
        start_analysis,
    ) -> None:
        sync_market.return_value = {"updated_rows": 0, "items": []}
        app_module.run_overview_sync()
        start_analysis.assert_not_called()

    def test_overview_has_separate_analysis_refresh_buttons(self) -> None:
        html = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertEqual(html.count('id="overview-refresh-all"'), 1)
        self.assertEqual(html.count('id="analysis-refresh-trendlines"'), 1)
        self.assertEqual(html.count('id="analysis-refresh-key-zones"'), 1)
        self.assertIn('title="刷新全部标的直线趋势线"', html)
        self.assertIn('title="刷新全部标的关键区域"', html)
        self.assertNotIn('id="market-refresh-all"', html)
        self.assertNotIn('id="backtest-create-mode"', html)
        self.assertNotIn('id="backtest-create-code"', html)

    @patch.object(app_module, "start_analysis_overview_refresh", return_value=True)
    def test_refresh_route_starts_only_requested_analysis(self, start) -> None:
        response = app_module.app.test_client().post(
            "/api/analysis-overview/refresh",
            json={"analysis_type": "key_zone"},
        )

        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with("key_zone")

    def test_refresh_route_rejects_unknown_analysis_type(self) -> None:
        response = app_module.app.test_client().post(
            "/api/analysis-overview/refresh",
            json={"analysis_type": "both"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "INVALID_ANALYSIS_TYPE",
        )


if __name__ == "__main__":
    unittest.main()
