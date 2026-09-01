from __future__ import annotations

import unittest
from unittest.mock import patch

from services.market_context import (
    annotate_us_market_sessions,
    filter_rows_for_market,
    is_cash_placeholder_symbol,
    normalize_market_config,
    uses_previous_close_for_historical_intraday,
)


class MarketContextTests(unittest.TestCase):
    def test_us_market_filter_excludes_weekend_and_new_year_holiday(self) -> None:
        rows = [
            {"date": "2023-12-29", "close": 100},
            {"date": "2023-12-30", "close": 101},
            {"date": "2024-01-01", "close": 102},
            {"date": "2024-01-02", "close": 103},
        ]
        sessions = [
            {"trading_date": "2023-12-29"},
            {"trading_date": "2024-01-02"},
        ]

        with patch(
            "services.backtest.market_calendar.ensure_market_sessions",
            return_value=sessions,
        ):
            annotated = annotate_us_market_sessions(rows)
            filtered = filter_rows_for_market(rows)

        flags = {
            row["date"]: row["is_us_market_session"] for row in annotated
        }
        self.assertTrue(flags["2023-12-29"])
        self.assertFalse(flags["2023-12-30"])
        self.assertFalse(flags["2024-01-01"])
        self.assertTrue(flags["2024-01-02"])
        self.assertEqual(
            [row["date"] for row in filtered],
            ["2023-12-29", "2024-01-02"],
        )

    def test_market_type_owns_calendar_and_timezone(self) -> None:
        market = normalize_market_config({
            "type": "US_EQUITY",
            "calendar": "wrong",
            "timezone": "Asia/Shanghai",
        })

        self.assertEqual(market["calendar"], "XNYS")
        self.assertEqual(market["timezone"], "America/New_York")

    def test_usdindex_and_us10y_are_us_cash_placeholders(self) -> None:
        self.assertTrue(is_cash_placeholder_symbol("USDIndex", "US_EQUITY"))
        self.assertTrue(is_cash_placeholder_symbol("us10y", {"type": "US_EQUITY"}))
        self.assertFalse(is_cash_placeholder_symbol("SPY", "US_EQUITY"))
        self.assertTrue(
            uses_previous_close_for_historical_intraday("US10Y", "US_EQUITY")
        )
        self.assertFalse(
            uses_previous_close_for_historical_intraday("USDINDEX", "US_EQUITY")
        )


if __name__ == "__main__":
    unittest.main()
