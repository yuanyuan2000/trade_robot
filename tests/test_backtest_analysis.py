from __future__ import annotations

from datetime import date, timedelta
import unittest
from unittest.mock import patch

from services.backtest import analysis
from services.backtest.errors import BacktestValidationError


def _dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    return [(current + timedelta(days=index)).isoformat() for index in range(count)]


def _snapshot(
    *,
    status: str = "completed",
    dates: list[str] | None = None,
    benchmark: str = "auto",
    design_mode: str = "visual",
    selection_mode: str = "competition",
    code_key: str | None = None,
) -> dict:
    dates = dates or _dates("2024-01-01", 10)
    return {
        "run": {
            "id": 7,
            "status": status,
            "strategy_name": "分析测试",
            "completed_at": "2024-04-01 16:00:00" if status == "completed" else None,
            "settings": {"benchmark": benchmark},
            "data_manifest": {"symbols": {}, "corporate_actions": []},
            "strategy_snapshot": {
                "design_mode": design_mode,
                "selection_mode": selection_mode,
                "code_key": code_key,
                "market": {"type": "US_EQUITY"},
                "definition": {
                    "symbols": [{"symbol": "AAA"}, {"symbol": "BBB"}],
                    "rules": [],
                },
            },
        },
        "equity_points": [
            {"trading_date": value, "equity": 1000 + index * 10}
            for index, value in enumerate(dates)
        ],
        "trades": [],
        "logs": [],
    }


def _price_rows(dates: list[str], *, multiplier: float = 1.0) -> list[dict]:
    return [
        {
            "date": value,
            "open": multiplier * (100 + index),
            "high": multiplier * (102 + index),
            "low": multiplier * (99 + index),
            "close": multiplier * (101 + index),
            "volume": 1000,
        }
        for index, value in enumerate(dates)
    ]


class BacktestAnalysisTests(unittest.TestCase):
    def tearDown(self) -> None:
        analysis.purge_analysis_cache([7])

    def test_running_run_unlocks_only_after_three_completed_calendar_months(self) -> None:
        before = _snapshot(status="running", dates=_dates("2024-01-01", 90))
        ready = _snapshot(status="running", dates=_dates("2024-01-01", 91))

        self.assertFalse(analysis.build_analysis_meta(before)["available"])
        self.assertTrue(analysis.build_analysis_meta(ready)["available"])
        with self.assertRaises(BacktestValidationError):
            analysis.build_analysis(before, "2024-01-01", "2024-03-30")

    def test_completed_short_run_is_available_and_range_uses_actual_dates(self) -> None:
        dates = _dates("2024-02-01", 12)
        snapshot = _snapshot(dates=dates)
        rows = _price_rows(dates)
        with patch.object(analysis, "_daily_rows", return_value=(rows, None)):
            payload = analysis.build_analysis(snapshot, "2024-01-01", "2024-05-01")

        self.assertTrue(analysis.build_analysis_meta(snapshot)["available"])
        self.assertEqual(payload["range"]["actual_start_date"], dates[0])
        self.assertEqual(payload["range"]["actual_end_date"], dates[-1])

    def test_pool_benchmark_is_not_duplicated_and_auto_is_equal_weight(self) -> None:
        dates = _dates("2024-01-01", 8)
        rows_a = _price_rows(dates)
        rows_b = _price_rows(dates, multiplier=2)

        def daily_rows(_run: dict, symbol: str):
            return (rows_a if symbol == "AAA" else rows_b), None

        pool_snapshot = _snapshot(dates=dates, benchmark="AAA")
        with patch.object(analysis, "_daily_rows", side_effect=daily_rows):
            pool_payload = analysis.build_analysis(pool_snapshot, dates[0], dates[-1])
        keys = [item["key"] for item in pool_payload["series"]]
        self.assertEqual(keys.count("asset:AAA"), 1)
        self.assertFalse(any(key.startswith("benchmark:") for key in keys))

        analysis.purge_analysis_cache([7])
        auto_snapshot = _snapshot(dates=dates, benchmark="auto")
        with patch.object(analysis, "_daily_rows", side_effect=daily_rows):
            auto_payload = analysis.build_analysis(auto_snapshot, dates[0], dates[-1])
        equal = next(item for item in auto_payload["series"] if item["key"] == "pool:equal")
        self.assertTrue(equal["configured_benchmark"])
        self.assertEqual(equal["points"][0]["return_rate"], 0)

    def test_leveraged_benchmarks_use_overall_and_per_symbol_leverage(self) -> None:
        dates = _dates("2024-01-01", 2)
        snapshot = _snapshot(
            dates=dates,
            design_mode="code",
            code_key="rapid_drop_wtme_rotation",
        )
        snapshot["run"]["settings"]["leverage_multiplier"] = 2
        definition = snapshot["run"]["strategy_snapshot"]["definition"]
        definition["symbols"] = [
            {"symbol": "AAA", "leverage_multiplier": 3},
            {"symbol": "BBB", "leverage_multiplier": 2},
        ]
        definition["params"] = {"allocation_mode": "leveraged_equal"}
        rows = [
            {"date": dates[0], "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
            {"date": dates[1], "open": 110, "high": 110, "low": 110, "close": 110, "volume": 1},
        ]

        with patch.object(analysis, "_daily_rows", return_value=(rows, None)):
            payload = analysis.build_analysis(snapshot, dates[0], dates[-1])

        aaa = next(item for item in payload["series"] if item["key"] == "asset:AAA")
        bbb = next(item for item in payload["series"] if item["key"] == "asset:BBB")
        equal = next(item for item in payload["series"] if item["key"] == "pool:equal")
        self.assertAlmostEqual(aaa["points"][-1]["return_rate"], 0.1)
        self.assertAlmostEqual(aaa["leveraged_points"][-1]["return_rate"], 0.6)
        self.assertAlmostEqual(bbb["leveraged_points"][-1]["return_rate"], 0.4)
        self.assertAlmostEqual(equal["leveraged_points"][-1]["return_rate"], 0.5)
        self.assertEqual(aaa["leverage_multiplier"], 6)
        self.assertTrue(
            payload["benchmark_leverage"]["dynamic_special_assumed_one"]
        )
        self.assertEqual(payload["benchmark_leverage"]["special_multiplier"], 1)

    def test_code_competition_decision_is_sorted_and_uses_compact_formula(self) -> None:
        snapshot = _snapshot(
            design_mode="code",
            code_key="sevenstar_etf_rotation",
        )
        snapshot["logs"] = [
            {
                "event_time": "2024-01-03 CLOSE",
                "event_type": "SEVENSTAR_DAILY_SCORE",
                "symbol": "AAA",
                "context": {
                    "symbol": "AAA",
                    "score": 1.2,
                    "eligible": True,
                    "annualized_returns": 3.2792,
                    "r_squared": 0.86,
                },
            },
            {
                "event_time": "2024-01-03 CLOSE",
                "event_type": "SEVENSTAR_DAILY_SCORE",
                "symbol": "BBB",
                "context": {
                    "symbol": "BBB",
                    "score": 2.4,
                    "eligible": False,
                    "filter_reasons": ["趋势过滤"],
                    "annualized_returns": 1.5,
                    "r_squared": 0.8,
                },
            },
        ]

        payload = analysis.build_decision(snapshot, "2024-01-03")

        self.assertEqual([row["symbol"] for row in payload["rows"]], ["BBB", "AAA"])
        self.assertTrue(payload["rows"][0]["filtered"])
        self.assertEqual(payload["rows"][1]["formula"], "327.9% × 0.86")
        self.assertEqual(payload["formula_help"], "评分 = 长期年化趋势 × R²")

    def test_visual_competition_decision_merges_filter_and_resolved_score(self) -> None:
        snapshot = _snapshot()
        snapshot["logs"] = [
            {
                "event_time": "2024-01-03 09:50",
                "event_type": "COMPETITION_ELIGIBILITY",
                "symbol": "AAA",
                "context": {"matched": True},
            },
            {
                "event_time": "2024-01-03 10:00",
                "event_type": "COMPETITION_SCORE",
                "symbol": "AAA",
                "context": {
                    "score": 1.1,
                    "formula": "price / ma(20)",
                    "inputs": {"price": 110, "ma(20)": 100},
                    "passes_minimum_score": True,
                },
            },
            {
                "event_time": "2024-01-03 09:50",
                "event_type": "COMPETITION_ELIGIBILITY",
                "symbol": "BBB",
                "context": {"matched": False, "reason": "候选条件未通过"},
            },
        ]

        payload = analysis.build_decision(snapshot, "2024-01-03")

        self.assertEqual(payload["rows"][0]["symbol"], "AAA")
        self.assertEqual(payload["rows"][0]["formula"], "110 ÷ 100")
        self.assertTrue(payload["rows"][1]["filtered"])

    def test_visual_non_competition_decision_shows_rule_result(self) -> None:
        snapshot = _snapshot(selection_mode="single")
        snapshot["run"]["strategy_snapshot"]["definition"]["rules"] = [{
            "id": "buy",
            "action": "BUY",
            "sizing_mode": "TARGET",
            "value": 50,
            "condition": "price > ma(20)",
            "when": "CLOSE",
        }]
        snapshot["logs"] = [{
            "event_time": "2024-01-03 CLOSE",
            "event_type": "RULE_EVALUATION",
            "symbol": "AAA",
            "context": {
                "rule_id": "buy",
                "condition": "price > ma(20)",
                "inputs": {"price": 110, "ma(20)": 100},
                "matched": True,
            },
        }]

        payload = analysis.build_decision(snapshot, "2024-01-03")

        self.assertEqual(payload["mode"], "rules")
        self.assertTrue(payload["rows"][0]["matched"])
        self.assertEqual(payload["rows"][0]["resolved_condition"], "110 > 100")

    def test_candles_are_raw_and_only_allow_pool_symbols(self) -> None:
        dates = _dates("2024-01-01", 5)
        snapshot = _snapshot(dates=dates)
        snapshot["trades"] = [
            {"event_time": "2024-01-02 10:00", "symbol": "AAA", "side": "BUY", "commission": 1, "slippage_amount": 0.2},
            {"event_time": "2024-01-04 10:00", "symbol": "AAA", "side": "SELL", "realized_pnl": 12, "commission": 1, "slippage_amount": 0.3},
        ]
        rows = _price_rows(dates)
        with patch.object(analysis, "_daily_rows", return_value=(rows, None)):
            payload = analysis.build_candles(snapshot, "AAA", dates[0], dates[-1])

        self.assertEqual(payload["candles"][0]["close"], rows[0]["close"])
        self.assertEqual(payload["summary"]["realized_pnl"], 12)
        self.assertEqual(payload["summary"]["profitable_sell_count"], 1)
        self.assertEqual(payload["summary"]["net_realized_pnl"], 9.5)
        self.assertEqual(payload["summary"]["starting_equity"], 1000)
        self.assertAlmostEqual(payload["summary"]["return_rate"], 0.0095)
        self.assertEqual(payload["summary"]["win_rate"], 1)
        with self.assertRaises(BacktestValidationError):
            analysis.build_candles(snapshot, "OUTSIDE", dates[0], dates[-1])


if __name__ == "__main__":
    unittest.main()
