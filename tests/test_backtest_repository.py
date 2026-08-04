from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
from database import backtest_repository
from services.backtest.presets import shipped_strategy_presets
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

    def test_builtin_code_strategy_cannot_be_deleted(self) -> None:
        seed_key, payload = next(
            item for item in shipped_strategy_presets()
            if item[1]["design_mode"] == "code"
        )
        strategy = backtest_repository.seed_strategy_once(seed_key, payload)

        with self.assertRaisesRegex(ValueError, "代码模式策略禁止删除"):
            backtest_repository.delete_strategy(strategy["id"])

        self.assertEqual(
            backtest_repository.get_strategy(strategy["id"])["id"],
            strategy["id"],
        )

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
        run = backtest_repository.create_run(
            strategy,
            settings,
            configuration_summary="测试运行参数摘要。",
        )

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
        self.assertEqual(stored["configuration_summary"], "测试运行参数摘要。")
        backtest_repository.delete_strategy(strategy["id"])
        stored_after_delete = backtest_repository.get_run(run["id"])
        self.assertIsNone(stored_after_delete["strategy_id"])
        self.assertEqual(
            stored_after_delete["strategy_snapshot"]["name"],
            "快照测试",
        )

    def test_seeded_code_version_upgrade_preserves_customization_and_history(self) -> None:
        _, shipped = next(
            item
            for item in shipped_strategy_presets()
            if item[0] == "builtin-sevenstar-etf-rotation-small-v1"
        )
        legacy = deepcopy(shipped)
        legacy["name"] = "七星迁移测试"
        legacy["code_version"] = "1.0.0"
        legacy["definition"]["symbols"] = [
            {"symbol": "GLD", "max_weight": 100},
            {"symbol": "SPY", "max_weight": 100},
        ]
        legacy["definition"]["params"]["max_score_threshold"] = 88.0
        legacy["default_settings"]["initial_capital"] = 123_456
        seed_key = "test-sevenstar-v1"
        seeded = backtest_repository.seed_strategy_once(seed_key, legacy)
        historical_run = backtest_repository.create_run(
            seeded, seeded["default_settings"]
        )

        upgraded = backtest_repository.upgrade_seeded_strategy_code_version_once(
            seed_key,
            "test-effective-w2-r2-v1.0.1",
            code_key="sevenstar_etf_rotation",
            from_versions=("1.0.0",),
            to_version="1.0.1",
        )

        self.assertEqual(upgraded["code_version"], "1.0.1")
        self.assertEqual(upgraded["revision"], 2)
        self.assertEqual(upgraded["definition"], legacy["definition"])
        self.assertEqual(upgraded["default_settings"], legacy["default_settings"])
        self.assertEqual(
            backtest_repository.get_run(historical_run["id"])["strategy_snapshot"][
                "code_version"
            ],
            "1.0.0",
        )

        self.assertIsNone(
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "test-effective-w2-r2-v1.0.1",
                code_key="sevenstar_etf_rotation",
                from_versions=("1.0.0",),
                to_version="1.0.1",
            )
        )
        self.assertEqual(
            backtest_repository.get_strategy(seeded["id"])["revision"], 2
        )

        upgraded_again = (
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "test-formula-mode-v1.1.0",
                code_key="sevenstar_etf_rotation",
                from_versions=("1.0.0", "1.0.1"),
                to_version="1.1.0",
                parameter_defaults={"trend_formula_mode": "consistent_w2"},
            )
        )
        expected_definition = deepcopy(legacy["definition"])
        expected_definition["params"]["trend_formula_mode"] = "consistent_w2"
        self.assertEqual(upgraded_again["code_version"], "1.1.0")
        self.assertEqual(upgraded_again["revision"], 3)
        self.assertEqual(upgraded_again["definition"], expected_definition)
        self.assertEqual(upgraded_again["default_settings"], legacy["default_settings"])
        self.assertIsNone(
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "test-formula-mode-v1.1.0",
                code_key="sevenstar_etf_rotation",
                from_versions=("1.0.0", "1.0.1"),
                to_version="1.1.0",
                parameter_defaults={"trend_formula_mode": "consistent_w2"},
            )
        )
        self.assertEqual(
            backtest_repository.get_strategy(seeded["id"])["revision"], 3
        )

    def test_seeded_code_version_upgrade_can_remove_retired_parameters(self) -> None:
        _, shipped = next(
            item
            for item in shipped_strategy_presets()
            if item[0] == "builtin-rapid-drop-wtme-rotation-v1"
        )
        legacy = deepcopy(shipped)
        legacy["name"] = "WTME 参数迁移测试"
        legacy["code_version"] = "1.0.0"
        legacy["definition"]["params"].update({
            "enable_atr_drop_filter": False,
            "drop_threshold_atr": 2.0,
            "atr_period": 5,
            "atr_weighting": "wilder",
        })
        seed_key = "test-wtme-remove-atr-params"
        backtest_repository.seed_strategy_once(seed_key, legacy)

        upgraded = backtest_repository.upgrade_seeded_strategy_code_version_once(
            seed_key,
            "test-wtme-remove-atr-params-v1.1.0",
            code_key="rapid_drop_wtme_rotation",
            from_versions=("1.0.0",),
            to_version="1.1.0",
            removed_parameters=(
                "enable_atr_drop_filter",
                "drop_threshold_atr",
                "atr_period",
                "atr_weighting",
            ),
        )

        self.assertEqual(upgraded["code_version"], "1.1.0")
        self.assertEqual(
            upgraded["definition"]["params"],
            shipped["definition"]["params"],
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
                    "borrowed_cash": 25,
                    "gross_leverage": 1.5,
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
        point = backtest_repository.get_equity_points(run["id"])[0]
        self.assertEqual(point["borrowed_cash"], 25)
        self.assertEqual(point["gross_leverage"], 1.5)
        self.assertEqual(
            backtest_repository.get_trades(run["id"])[0]["symbol"],
            "SPY",
        )
        self.assertEqual(
            backtest_repository.get_logs(run["id"], level="INFO")[0]["message"],
            "买入",
        )
        updated_run = backtest_repository.update_run(
            run["id"],
            status="completed",
            termination_reason="LIQUIDATED",
        )
        self.assertEqual(updated_run["termination_reason"], "LIQUIDATED")

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
            "legs": [
                {
                    "role": "subject",
                    "symbol": "SPY",
                    "cusip": "78462F103",
                    "isin": "US78462F1030",
                }
            ],
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
        with main_db.get_connection() as conn:
            symbol_mapping = conn.execute(
                "SELECT instrument_key FROM instrument_symbols WHERE symbol = 'SPY'"
            ).fetchone()
            identifier = conn.execute(
                """
                SELECT instrument_key FROM instrument_identifiers
                WHERE identifier_type = 'cusip' AND identifier_value = '78462F103'
                """
            ).fetchone()
        self.assertEqual(symbol_mapping["instrument_key"], "cusip:78462F103")
        self.assertEqual(identifier["instrument_key"], "cusip:78462F103")

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

    def test_run_soft_delete_hides_summary_and_removes_heavy_rows(self) -> None:
        strategy = create_default_strategy(
            name="日志清理测试",
            design_mode="visual",
            selection_mode="single",
        )
        run = backtest_repository.create_run(strategy, strategy["default_settings"])
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[
                {
                    "trading_date": "2024-01-02",
                    "cash": 100,
                    "receivables": 0,
                    "positions_value": 0,
                    "equity": 100,
                    "return_rate": 0,
                    "drawdown_rate": 0,
                    "benchmark_equity": None,
                    "benchmark_return_rate": None,
                    "positions": {"SPY": {"quantity": 1}},
                }
            ],
            trades=[],
            logs=[
                {
                    "level": "INFO",
                    "event_type": "TEST",
                    "message": "需要清理的日志",
                    "context": {"value": 1},
                }
            ],
        )
        backtest_repository.update_run(
            run["id"], status="completed", metrics={"total_return": 0.1}
        )

        overview = backtest_repository.list_runs_overview(page=1, page_size=10)
        self.assertEqual(overview["total_rows"], 1)
        self.assertEqual(overview["items"][0]["symbols"], ["SPY"])
        self.assertEqual(
            overview["items"][0]["settings"]["leverage_multiplier"], 1.0
        )
        detail = backtest_repository.get_run_detail(run["id"])
        self.assertEqual(detail["strategy_snapshot"]["name"], "日志清理测试")
        self.assertEqual(detail["available_log_count"], 1)

        deleted = backtest_repository.delete_runs([run["id"]])

        self.assertEqual(deleted["deleted_log_rows"], 1)
        self.assertEqual(deleted["deleted_equity_rows"], 1)
        self.assertEqual(backtest_repository.get_logs(run["id"]), [])
        self.assertEqual(backtest_repository.get_equity_points(run["id"]), [])
        self.assertEqual(
            backtest_repository.list_runs_overview(page=1, page_size=10)["total_rows"],
            0,
        )
        with self.assertRaises(ValueError):
            backtest_repository.get_run_detail(run["id"])
        retained = backtest_repository.get_run(run["id"], include_deleted=True)
        self.assertIsNotNone(retained["deleted_at"])
        self.assertEqual(retained["metrics"]["total_return"], 0.1)
        self.assertEqual(retained["strategy_snapshot"]["name"], "日志清理测试")


if __name__ == "__main__":
    unittest.main()
