from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import database.db as main_db
from database import backtest_repository
from services.backtest.engine import BacktestResult
from services.backtest.errors import BacktestCancelled, BacktestDataError
from services.backtest.service import BacktestRunManager, create_default_strategy


class FakeEngine:
    def __init__(
        self,
        strategy,
        settings,
        *,
        dataset=None,
        progress_callback=None,
        cancellation_check=None,
    ):
        self.strategy = strategy
        self.settings = settings
        self.progress_callback = progress_callback
        self.cancellation_check = cancellation_check
        self.dataset = dataset or type(
            "Dataset",
            (),
            {"manifest": {"test": True}, "sessions": ["2024-01-02"]},
        )()
        self.equity_points = []
        self.trades = []
        self.logs = []

    def run(self):
        point = {
            "trading_date": "2024-01-02",
            "cash": 100,
            "receivables": 0,
            "positions_value": 0,
            "equity": 100,
            "return_rate": 0,
            "drawdown_rate": 0,
            "benchmark_equity": None,
            "benchmark_return_rate": None,
            "positions": {},
        }
        self.equity_points.append(point)
        self.logs.append(
            {
                "event_time": "2024-01-02 CLOSE",
                "level": "INFO",
                "event_type": "RUN_COMPLETE",
                "message": "完成",
            }
        )
        self.progress_callback(
            {
                "progress": 1,
                "current_time": "2024-01-02",
                "equity_point": point,
                "trade_count": 0,
                "log_count": 1,
            }
        )
        return BacktestResult(
            metrics={"ending_equity": 100, "total_return": 0},
            equity_points=self.equity_points,
            trades=self.trades,
            logs=self.logs,
            data_manifest=self.dataset.manifest,
        )


class FailingPreflightEngine:
    def __init__(self, *args, **kwargs):
        raise BacktestDataError(
            "公司行动核验失败。",
            detail={"provider": "test"},
        )


class LiveMetricsEngine(FakeEngine):
    progress_reported = threading.Event()
    release = threading.Event()

    def run(self):
        result = super().run()
        self.progress_reported.set()
        self.release.wait(timeout=2)
        return result


class LiquidatedEngine(FakeEngine):
    def run(self):
        result = super().run()
        result.metrics.update(
            {
                "ending_equity": -5,
                "total_return": -1.05,
                "liquidated": True,
            }
        )
        result.termination_reason = "LIQUIDATED"
        result.liquidation = {
            "liquidation_time": "2024-01-02 10:00 America/New_York"
        }
        return result


class CancellableEngine:
    def __init__(
        self,
        strategy,
        settings,
        *,
        dataset=None,
        progress_callback=None,
        cancellation_check=None,
    ):
        self.dataset = dataset or type(
            "Dataset",
            (),
            {"manifest": {"test": True}, "sessions": ["2024-01-02"]},
        )()
        self.progress_callback = progress_callback
        self.cancellation_check = cancellation_check
        self.equity_points = []
        self.trades = []
        self.logs = []

    def run(self):
        while not self.cancellation_check():
            time.sleep(0.005)
        raise BacktestCancelled("用户取消了回测。")

    def _log(self, level, event_type, message):
        self.logs.append(
            {
                "level": level,
                "event_type": event_type,
                "message": message,
            }
        )


class BacktestRunManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.db_patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.db_patcher.start()
        main_db.init_database()
        self.manager = BacktestRunManager()
        self.strategy = create_default_strategy(
            name="后台任务测试",
            design_mode="visual",
            selection_mode="single",
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    @patch("services.backtest.service.BacktestEngine", FakeEngine)
    def test_background_run_persists_progress_result_and_logs(self) -> None:
        run = self.manager.start(
            self.strategy["id"],
            {
                **self.strategy["default_settings"],
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "initial_capital": 100,
                "benchmark": "none",
            },
        )
        deadline = time.monotonic() + 2
        status = run
        while time.monotonic() < deadline:
            status = self.manager.run_status(run["id"])
            if status["status"] == "completed":
                break
            time.sleep(0.01)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["metrics"]["ending_equity"], 100)
        self.assertIn("OPEN若", status["configuration_summary"])
        self.assertEqual(
            backtest_repository.get_equity_points(run["id"])[0]["equity"],
            100,
        )
        self.assertEqual(
            backtest_repository.get_logs(run["id"], level="INFO")[0]["message"],
            "完成",
        )
        range_snapshot = self.manager.analysis_snapshot(run["id"])
        decision_snapshot = self.manager.analysis_snapshot(
            run["id"],
            trading_date="2024-01-02",
        )
        self.assertEqual(range_snapshot["logs"], [])
        self.assertEqual(len(decision_snapshot["logs"]), 1)
        self.assertEqual(decision_snapshot["logs"][0]["event_type"], "RUN_COMPLETE")

    @patch("services.backtest.service.BacktestEngine", LiveMetricsEngine)
    def test_running_status_exposes_live_metrics(self) -> None:
        LiveMetricsEngine.progress_reported.clear()
        LiveMetricsEngine.release.clear()
        run = self.manager.start(
            self.strategy["id"],
            {
                **self.strategy["default_settings"],
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "initial_capital": 100,
                "benchmark": "none",
            },
        )
        try:
            self.assertTrue(LiveMetricsEngine.progress_reported.wait(timeout=1))
            status = self.manager.run_status(run["id"])

            self.assertEqual(status["status"], "running")
            self.assertEqual(status["metrics"]["total_return"], 0)
            self.assertEqual(status["metrics"]["trade_count"], 0)
            self.assertEqual(status["live"]["equity_point_count"], 1)
        finally:
            LiveMetricsEngine.release.set()

    @patch("services.backtest.service.BacktestEngine", FailingPreflightEngine)
    def test_preflight_failure_does_not_create_run_record(self) -> None:
        with self.assertRaisesRegex(BacktestDataError, "公司行动核验失败"):
            self.manager.start(
                self.strategy["id"],
                {
                    **self.strategy["default_settings"],
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "initial_capital": 100,
                    "benchmark": "none",
                },
            )
        self.assertEqual(backtest_repository.list_runs(self.strategy["id"]), [])

    @patch("services.backtest.service.BacktestEngine", LiquidatedEngine)
    def test_liquidation_is_completed_with_full_result_and_outcome(self) -> None:
        run = self.manager.start(
            self.strategy["id"],
            {
                **self.strategy["default_settings"],
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "initial_capital": 100,
                "benchmark": "none",
                "leverage_multiplier": 3,
            },
        )
        deadline = time.monotonic() + 2
        status = run
        while time.monotonic() < deadline:
            status = self.manager.run_status(run["id"])
            if status["status"] == "completed":
                break
            time.sleep(0.01)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["termination_reason"], "LIQUIDATED")
        self.assertTrue(status["metrics"]["liquidated"])
        self.assertIn("10:00", status["current_time"])
        self.assertEqual(len(self.manager.result(run["id"])["equity_points"]), 1)

    def test_terminal_partial_output_is_readable_after_manager_restart(self) -> None:
        run = backtest_repository.create_run(
            self.strategy,
            self.strategy["default_settings"],
        )
        point = {
            "trading_date": "2024-01-02",
            "cash": 90,
            "receivables": 0,
            "positions_value": 10,
            "equity": 100,
            "return_rate": 0,
            "drawdown_rate": 0,
            "benchmark_equity": None,
            "benchmark_return_rate": None,
            "positions": {"SPY": {"quantity": 1}},
        }
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[point],
            trades=[],
            logs=[],
        )
        backtest_repository.update_run(
            run["id"],
            status="failed",
            error_code="TEST",
            error_message="测试失败",
        )

        restarted_manager = BacktestRunManager()
        result = restarted_manager.result(run["id"])

        self.assertEqual(result["equity_points"][0]["equity"], 100)

    @patch("services.backtest.service.BacktestEngine", CancellableEngine)
    def test_running_job_can_be_cancelled(self) -> None:
        run = self.manager.start(
            self.strategy["id"],
            {
                **self.strategy["default_settings"],
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "initial_capital": 100,
                "benchmark": "none",
            },
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.manager.run_status(run["id"])["status"] == "running":
                break
            time.sleep(0.005)
        self.manager.cancel(run["id"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.manager.run_status(run["id"])
            if status["status"] == "cancelled":
                break
            time.sleep(0.005)

        self.assertEqual(status["status"], "cancelled")

    @patch("services.backtest.service.BacktestEngine", CancellableEngine)
    def test_cancel_request_cannot_overwrite_cancelled_terminal_status(self) -> None:
        run = self.manager.start(
            self.strategy["id"],
            {
                **self.strategy["default_settings"],
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "initial_capital": 100,
                "benchmark": "none",
            },
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.manager.run_status(run["id"])["status"] == "running":
                break
            time.sleep(0.005)

        original_request = backtest_repository.request_run_cancellation

        def request_after_worker_finishes(run_id: int) -> dict:
            deadline = time.monotonic() + 2
            status = backtest_repository.get_run(run_id)["status"]
            while time.monotonic() < deadline:
                status = backtest_repository.get_run(run_id)["status"]
                if status == "cancelled":
                    break
                time.sleep(0.005)
            self.assertEqual(status, "cancelled")
            return original_request(run_id)

        with patch.object(
            backtest_repository,
            "request_run_cancellation",
            side_effect=request_after_worker_finishes,
        ):
            cancelled = self.manager.cancel(run["id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            backtest_repository.get_run(run["id"])["status"],
            "cancelled",
        )


if __name__ == "__main__":
    unittest.main()
