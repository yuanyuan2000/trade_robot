from __future__ import annotations

import unittest
from unittest.mock import patch

from services.yahoo_finance_client import fetch_hourly_derived_daily_prices


class YahooFinanceClientTests(unittest.TestCase):
    @patch("services.yahoo_finance_client._fetch_chart_payload")
    def test_hourly_rows_can_rebuild_a_null_daily_bar(self, fetch_payload) -> None:
        fetch_payload.return_value = {
            "chart": {
                "result": [{
                    "timestamp": [1787875200, 1787878800],
                    "indicators": {"quote": [{
                        "open": [99.11, 99.50],
                        "high": [99.60, 99.73],
                        "low": [99.10, 99.40],
                        "close": [99.55, 99.70],
                    }]},
                }]
            }
        }

        rows = fetch_hourly_derived_daily_prices(
            "DX-Y.NYB",
            ["2026-08-28"],
        )

        self.assertEqual(rows[0]["date"], "2026-08-28")
        self.assertEqual(rows[0]["open"], 99.11)
        self.assertEqual(rows[0]["high"], 99.73)
        self.assertEqual(rows[0]["low"], 99.10)
        self.assertEqual(rows[0]["close"], 99.70)
        self.assertEqual(rows[0]["source_timeframe"], "60MinDerived")


if __name__ == "__main__":
    unittest.main()
