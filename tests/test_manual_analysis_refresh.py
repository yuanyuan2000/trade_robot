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

    def test_overview_has_manual_refresh_button(self) -> None:
        html = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="analysis-refresh-all"', html)
        self.assertNotIn('id="backtest-create-mode"', html)
        self.assertNotIn('id="backtest-create-code"', html)


if __name__ == "__main__":
    unittest.main()
