from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from services.backtest.corporate_actions import (
    ensure_corporate_actions,
    fetch_corporate_actions,
)
from services.backtest.market_calendar import (
    ensure_market_sessions,
    fetch_market_sessions,
)


class CorporateActionClientTests(unittest.TestCase):
    @patch("services.backtest.corporate_actions.ALPACA_API_KEY", "test-key")
    @patch("services.backtest.corporate_actions.ALPACA_SECRET", "test-secret")
    @patch("services.backtest.corporate_actions.requests.get")
    def test_split_and_dividend_payload_is_normalized(self, get: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "corporate_actions": {
                "forward_splits": [
                    {
                        "id": "split-id",
                        "symbol": "NVDA",
                        "new_rate": 10,
                        "old_rate": 1,
                        "process_date": "2024-06-10",
                        "ex_date": "2024-06-10",
                    }
                ],
                "cash_dividends": [
                    {
                        "id": "dividend-id",
                        "symbol": "NVDA",
                        "rate": 0.01,
                        "process_date": "2024-06-11",
                        "ex_date": "2024-06-11",
                        "payable_date": "2024-06-28",
                    }
                ],
            },
            "next_page_token": None,
        }
        get.return_value = response

        actions = fetch_corporate_actions(
            ["NVDA"],
            start_date="2024-01-01",
            end_date="2024-12-31",
        )

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["action_type"], "forward_split")
        self.assertEqual(actions[0]["new_rate"], 10)
        self.assertEqual(actions[1]["action_type"], "cash_dividend")
        self.assertEqual(actions[1]["cash_rate"], 0.01)
        self.assertEqual(actions[1]["payable_date"], "2024-06-28")

    @patch("services.backtest.market_calendar.ALPACA_API_KEY", "test-key")
    @patch("services.backtest.market_calendar.ALPACA_SECRET", "test-secret")
    @patch("services.backtest.market_calendar.requests.get")
    def test_market_calendar_preserves_dst_and_early_close(self, get: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {
                "date": "2024-07-03",
                "open": "09:30",
                "close": "13:00",
            }
        ]
        get.return_value = response

        sessions = fetch_market_sessions("2024-07-03", "2024-07-03")

        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["is_early_close"])
        self.assertEqual(
            sessions[0]["close_minute_utc"] - sessions[0]["open_minute_utc"],
            210,
        )

    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.get_corporate_actions",
        return_value=[],
    )
    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.upsert_corporate_actions"
    )
    @patch(
        "services.backtest.corporate_actions.fetch_corporate_actions",
        return_value=[],
    )
    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.corporate_action_coverage"
    )
    def test_extending_action_cache_refetches_continuous_union(
        self,
        coverage: Mock,
        fetch: Mock,
        upsert: Mock,
        _get: Mock,
    ) -> None:
        coverage.return_value = {
            "coverage_start": "2024-01-01",
            "coverage_end": "2024-03-31",
            "status": "success",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        ensure_corporate_actions(
            ["SPY", "GLD"],
            start_date="2024-06-01",
            end_date="2024-06-30",
        )

        fetch.assert_called_once_with(
            ["SPY", "GLD"],
            start_date="2024-01-01",
            end_date="2024-06-30",
        )
        self.assertEqual(
            upsert.call_args.kwargs["coverage_start"],
            "2024-01-01",
        )

    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.get_corporate_actions",
        return_value=[],
    )
    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.upsert_corporate_actions"
    )
    @patch(
        "services.backtest.corporate_actions.fetch_corporate_actions",
        return_value=[],
    )
    @patch(
        "services.backtest.corporate_actions."
        "backtest_repository.corporate_action_coverage"
    )
    def test_stale_complete_action_cache_is_reverified(
        self,
        coverage: Mock,
        fetch: Mock,
        _upsert: Mock,
        _get: Mock,
    ) -> None:
        coverage.return_value = {
            "coverage_start": "2024-01-01",
            "coverage_end": "2024-12-31",
            "status": "success",
            "synced_at": (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).isoformat(),
        }

        ensure_corporate_actions(
            ["SPY"],
            start_date="2024-06-01",
            end_date="2024-06-30",
        )

        fetch.assert_called_once_with(
            ["SPY"],
            start_date="2024-01-01",
            end_date="2024-12-31",
        )

    @patch(
        "services.backtest.market_calendar."
        "intraday_repository.get_market_sessions",
        return_value=[{"trading_date": "2024-06-03"}],
    )
    @patch(
        "services.backtest.market_calendar."
        "intraday_repository.upsert_market_sessions"
    )
    @patch(
        "services.backtest.market_calendar.fetch_market_sessions",
        return_value=[{"trading_date": "2024-06-03"}],
    )
    @patch(
        "services.backtest.market_calendar."
        "intraday_repository.get_market_calendar_coverage"
    )
    def test_extending_calendar_cache_refetches_continuous_union(
        self,
        coverage: Mock,
        fetch: Mock,
        upsert: Mock,
        _get: Mock,
    ) -> None:
        coverage.return_value = {
            "coverage_start": "2024-01-01",
            "coverage_end": "2024-03-31",
            "status": "success",
        }

        ensure_market_sessions("2024-06-01", "2024-06-30")

        fetch.assert_called_once_with("2024-01-01", "2024-06-30")
        self.assertEqual(
            upsert.call_args.kwargs["coverage_start"],
            "2024-01-01",
        )


if __name__ == "__main__":
    unittest.main()
