from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
from database import backtest_repository
from services.backtest.service import create_default_strategy


class BacktestRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.patchers = [
            patch.object(main_db, "DATABASE_PATH", self.database_path),
            patch.object(main_db, "DATA_DIR", self.data_dir),
        ]
        for patcher in self.patchers:
            patcher.start()
        main_db.init_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_strategy_create_update_hard_delete_and_revision(self) -> None:
        strategy = create_default_strategy(
            name="均线测试",
            design_mode="visual",
            selection_mode="single",
        )
        self.assertEqual(strategy["revision"], 1)
        self.assertEqual(strategy["definition"]["symbols"][0]["symbol"], "SPY")

        updated = backtest_repository.update_strategy(
            strategy["id"],
            {**strategy, "name": "均线测试 v2"},
            expected_revision=1,
        )
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(RuntimeError):
            backtest_repository.update_strategy(
                strategy["id"],
                updated,
                expected_revision=1,
            )

        backtest_repository.delete_strategy(strategy["id"])
        self.assertEqual(backtest_repository.list_strategies(), [])
        self.assertEqual(backtest_repository.list_strategies(include_deleted=True), [])
        with self.assertRaises(ValueError):
            backtest_repository.get_strategy(strategy["id"], include_deleted=True)

    def test_run_keeps_immutable_strategy_and_settings_snapshot(self) -> None:
        strategy = create_default_strategy(
            name="快照测试",
            design_mode="visual",
            selection_mode="single",
        )
        settings = {
            **strategy["default_settings"],
            "initial_capital": 12345,
        }
        run = backtest_repository.create_run(strategy, settings)

        backtest_repository.update_strategy(
            strategy["id"],
            {**strategy, "name": "已修改名称"},
            expected_revision=1,
        )
        stored = backtest_repository.get_run(run["id"])
        self.assertEqual(stored["strategy_name"], "快照测试")
        self.assertEqual(stored["strategy_revision"], 1)
        self.assertEqual(stored["strategy_snapshot"]["name"], "快照测试")
        self.assertEqual(stored["settings"]["initial_capital"], 12345)
        backtest_repository.delete_strategy(strategy["id"])
        stored_after_delete = backtest_repository.get_run(run["id"])
        self.assertIsNone(stored_after_delete["strategy_id"])
        self.assertEqual(
            stored_after_delete["strategy_snapshot"]["name"],
            "快照测试",
        )

    def test_output_round_trip_preserves_equity_trade_and_log(self) -> None:
        strategy = create_default_strategy(
            name="结果测试",
            design_mode="visual",
            selection_mode="single",
        )
        run = backtest_repository.create_run(
            strategy,
            strategy["default_settings"],
        )
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[
                {
                    "trading_date": "2024-01-02",
                    "cash": 1,
                    "positions_value": 99,
                    "equity": 100,
                    "return_rate": 0,
                    "drawdown_rate": 0,
                    "benchmark_equity": None,
                    "benchmark_return_rate": None,
                    "positions": {"SPY": {"quantity": 1}},
                }
            ],
            trades=[
                {
                    "event_time": "2024-01-02 OPEN",
                    "symbol": "SPY",
                    "side": "BUY",
                    "quantity": 1,
                    "reference_price": 99,
                    "fill_price": 99,
                    "gross_amount": 99,
                    "commission": 0,
                    "slippage_amount": 0,
                    "realized_pnl": None,
                    "cash_after": 1,
                    "position_quantity_after": 1,
                    "position_value_after": 99,
                    "position_weight_after": 0.99,
                    "reason": "test",
                }
            ],
            logs=[
                {
                    "event_time": "2024-01-02 OPEN",
                    "level": "INFO",
                    "event_type": "TRADE",
                    "symbol": "SPY",
                    "message": "买入",
                    "context": {"quantity": 1},
                }
            ],
        )

        self.assertEqual(
            backtest_repository.get_equity_points(run["id"])[0]["equity"],
            100,
        )
        self.assertEqual(
            backtest_repository.get_trades(run["id"])[0]["symbol"],
            "SPY",
        )
        self.assertEqual(
            backtest_repository.get_logs(run["id"], level="INFO")[0]["message"],
            "买入",
        )

    def test_corporate_action_refresh_removes_provider_deletions(self) -> None:
        action = {
            "provider_id": "split-1",
            "provider": "alpaca",
            "action_type": "forward_split",
            "symbol": "SPY",
            "process_date": "2024-06-01",
            "ex_date": "2024-06-01",
            "old_rate": 1,
            "new_rate": 2,
            "payload": {},
        }
        backtest_repository.upsert_corporate_actions(
            [action],
            symbols=["SPY"],
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
        )
        self.assertEqual(
            len(
                backtest_repository.get_corporate_actions(
                    ["SPY"],
                    start_date="2024-01-01",
                    end_date="2024-12-31",
                )
            ),
            1,
        )

        backtest_repository.upsert_corporate_actions(
            [],
            symbols=["SPY"],
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
        )

        self.assertEqual(
            backtest_repository.get_corporate_actions(
                ["SPY"],
                start_date="2024-01-01",
                end_date="2024-12-31",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
