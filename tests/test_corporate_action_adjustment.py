from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from services.backtest.corporate_actions import (
    ensure_corporate_actions,
    validate_supported_actions,
)
from services.backtest.data import HistoricalDataSet
from services.corporate_action_adjustment_service import (
    adjust_price_rows,
    adjusted_daily_payload,
)


class CorporateActionIdentityTests(unittest.TestCase):
    @patch(
        "services.backtest.corporate_actions.backtest_repository.corporate_action_coverage"
    )
    @patch(
        "services.backtest.corporate_actions.backtest_repository.get_corporate_actions"
    )
    def test_mags_ticker_reuse_is_filtered_by_identity_start(
        self, get_actions, coverage
    ) -> None:
        coverage.return_value = {
            "coverage_start": "2020-01-01",
            "coverage_end": "2026-12-31",
            "status": "success",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        get_actions.return_value = [
            {
                "provider_id": "old-mags",
                "action_type": "name_change",
                "symbol": "MAGS",
                "process_date": "2021-09-30",
                "effective_date": "2021-09-30",
                "legs": [
                    {"role": "source", "symbol": "MAGS", "cusip": "81728N100"},
                    {"role": "target", "symbol": "SNT", "cusip": "81728N100"},
                ],
            },
            {
                "provider_id": "current-mags",
                "action_type": "name_change",
                "symbol": "BIGT",
                "process_date": "2023-11-09",
                "effective_date": "2023-11-09",
                "legs": [
                    {"role": "source", "symbol": "BIGT", "cusip": "53656G498"},
                    {"role": "target", "symbol": "MAGS", "cusip": "53656G498"},
                ],
            },
        ]

        actions = ensure_corporate_actions(
            ["MAGS"],
            start_date="2020-01-01",
            end_date="2026-07-31",
            symbol_starts={"MAGS": "2023-04-11"},
        )

        self.assertEqual([item["provider_id"] for item in actions], ["current-mags"])
        self.assertEqual(actions[0]["matched_role"], "target")
        self.assertFalse(actions[0]["affects_position"])
        validate_supported_actions(actions)

    def test_related_merger_and_spin_off_only_block_affected_side(self) -> None:
        harmless = [
            {"symbol": "NVDA", "action_type": "cash_merger", "process_date": "2020-04-27", "matched_role": "acquirer", "affects_position": False},
            {"symbol": "SNDK", "action_type": "spin_off", "process_date": "2025-02-24", "matched_role": "target", "affects_position": False},
        ]
        validate_supported_actions(harmless)
        with self.assertRaisesRegex(Exception, "暂不支持"):
            validate_supported_actions([
                {"symbol": "WDC", "action_type": "spin_off", "process_date": "2025-02-24", "matched_role": "source", "affects_position": True}
            ])

    def test_dividend_missing_payable_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "缺少关键字段"):
            validate_supported_actions([
                {
                    "provider_id": "incomplete-dividend",
                    "symbol": "INTC",
                    "action_type": "cash_dividend",
                    "process_date": "2020-02-06",
                    "cash_rate": 0.33,
                    "payable_date": None,
                    "affects_position": True,
                }
            ])


class CorporateActionAdjustmentTests(unittest.TestCase):
    def test_multiple_splits_compound_without_price_jump(self) -> None:
        rows = [
            {"date": "2021-01-04", "open": 400, "high": 404, "low": 396, "close": 400, "volume": 100},
            {"date": "2021-07-20", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 400},
            {"date": "2024-06-10", "open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 4000},
        ]
        actions = [
            {"symbol": "NVDA", "action_type": "forward_split", "effective_date": "2021-07-20", "old_rate": 1, "new_rate": 4, "affects_position": True},
            {"symbol": "NVDA", "action_type": "forward_split", "effective_date": "2024-06-10", "old_rate": 1, "new_rate": 10, "affects_position": True},
        ]

        adjusted = adjust_price_rows(rows, actions, mode="split")

        self.assertAlmostEqual(adjusted[0]["close"], 10)
        self.assertAlmostEqual(adjusted[1]["close"], 10)
        self.assertAlmostEqual(adjusted[0]["volume"], 4000)

    def test_same_day_dividends_are_accumulated(self) -> None:
        rows = [
            {"date": "2024-12-27", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 10},
            {"date": "2024-12-30", "open": 98, "high": 98, "low": 98, "close": 98, "volume": 10},
        ]
        actions = [
            {"symbol": "MAGS", "action_type": "cash_dividend", "effective_date": "2024-12-30", "cash_rate": 1.25, "affects_position": True},
            {"symbol": "MAGS", "action_type": "cash_dividend", "effective_date": "2024-12-30", "cash_rate": 0.75, "affects_position": True},
        ]

        adjusted = adjust_price_rows(rows, actions, mode="all")

        self.assertAlmostEqual(adjusted[0]["close"], 98)
        self.assertAlmostEqual(adjusted[1]["close"], 98)

    def test_backtest_indicator_history_adjusts_dividends_but_day_bar_stays_raw(self) -> None:
        rows = [
            {"date": "2024-12-27", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 10},
            {"date": "2024-12-30", "open": 98, "high": 98, "low": 98, "close": 98, "volume": 12},
        ]
        actions = [
            {"symbol": "MAGS", "action_type": "cash_dividend", "process_date": "2024-12-30", "cash_rate": 1.25, "payable_date": "2025-01-03", "affects_position": True},
            {"symbol": "MAGS", "action_type": "cash_dividend", "process_date": "2024-12-30", "cash_rate": 0.75, "payable_date": "2025-01-03", "affects_position": True},
        ]
        dataset = HistoricalDataSet(
            daily={"MAGS": rows},
            sessions=["2024-12-27", "2024-12-30"],
            corporate_actions=actions,
        )

        history = dataset.indicator_history(
            "MAGS", "2024-12-30", include_current=True
        )

        self.assertAlmostEqual(history[0]["close"], 98)
        self.assertAlmostEqual(history[1]["close"], 98)
        self.assertAlmostEqual(dataset.day_bar("MAGS", "2024-12-30")["close"], 98)

    def test_backtest_multiple_splits_are_point_in_time_without_future_leakage(self) -> None:
        rows = [
            {"date": "2021-01-04", "open": 400, "high": 400, "low": 400, "close": 400, "volume": 100},
            {"date": "2021-07-20", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 400},
            {"date": "2024-06-10", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 4000},
        ]
        actions = [
            {"symbol": "NVDA", "action_type": "forward_split", "process_date": "2021-07-20", "old_rate": 1, "new_rate": 4, "affects_position": True},
            {"symbol": "NVDA", "action_type": "forward_split", "process_date": "2024-06-10", "old_rate": 1, "new_rate": 10, "affects_position": True},
        ]
        dataset = HistoricalDataSet(
            daily={"NVDA": rows},
            sessions=[row["date"] for row in rows],
            corporate_actions=actions,
        )

        first_split_view = dataset.indicator_history(
            "NVDA", "2021-07-20", include_current=True
        )
        second_split_view = dataset.indicator_history(
            "NVDA", "2024-06-10", include_current=True
        )

        self.assertEqual([row["close"] for row in first_split_view], [100, 100])
        self.assertEqual([row["close"] for row in second_split_view], [10, 10, 10])

    @patch(
        "services.corporate_action_adjustment_service.ensure_corporate_actions"
    )
    def test_unknown_price_basis_is_not_adjusted_twice(self, ensure) -> None:
        rows = [
            {"date": "2021-02-11", "open": 31, "high": 32, "low": 30, "close": 31.5, "volume": 10, "price_basis": "unknown"},
            {"date": "2021-02-12", "open": 31.6, "high": 32, "low": 31, "close": 31.8, "volume": 40, "price_basis": "unknown"},
        ]

        payload = adjusted_daily_payload(
            "FNGS", rows, {"asset_class": "us_equity"}, mode="all"
        )

        ensure.assert_not_called()
        self.assertEqual(payload["adjustment"], "raw")
        self.assertEqual(payload["rows"][0]["close"], 31.5)
        self.assertIn("避免二次复权", payload["warning"])


if __name__ == "__main__":
    unittest.main()
