from __future__ import annotations

from concurrent.futures import Future
import logging
import unittest
from unittest.mock import Mock, patch

import app as app_module
from app import (
    HeartbeatAccessLogFilter,
    analysis_worker_count,
    terminate_analysis_process_executor,
    terminate_child_processes,
)


class ImmediateExecutor:
    def __init__(self, max_workers: int, mp_context) -> None:
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def submit(self, fn, *args) -> Future:
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class AnalysisParallelTests(unittest.TestCase):
    def test_heartbeat_access_log_is_suppressed(self) -> None:
        log_filter = HeartbeatAccessLogFilter()
        heartbeat = logging.LogRecord(
            "werkzeug",
            logging.INFO,
            "",
            0,
            '"POST /api/session/heartbeat HTTP/1.1" 200 -',
            (),
            None,
        )
        market_data = logging.LogRecord(
            "werkzeug",
            logging.INFO,
            "",
            0,
            '"GET /api/market-data HTTP/1.1" 200 -',
            (),
            None,
        )

        self.assertFalse(log_filter.filter(heartbeat))
        self.assertTrue(log_filter.filter(market_data))

    @patch.object(app_module.multiprocessing, "active_children")
    def test_shutdown_terminates_active_analysis_workers(
            self,
            active_children,
    ) -> None:
        worker = Mock()
        worker.is_alive.return_value = True
        active_children.return_value = [worker]

        terminate_child_processes()

        worker.terminate.assert_called_once_with()
        worker.join.assert_called_once_with(timeout=0.5)

    def test_shutdown_terminates_registered_executor_workers(self) -> None:
        worker = Mock()
        worker.is_alive.return_value = True
        executor = Mock()
        executor._processes = {101: worker}
        app_module.analysis_process_executor = executor

        terminate_analysis_process_executor()

        worker.terminate.assert_called_once_with()
        executor.shutdown.assert_called_once_with(
            wait=False,
            cancel_futures=True,
        )
        self.assertIsNone(app_module.analysis_process_executor)

    def test_worker_count_is_bounded_at_four(self) -> None:
        self.assertEqual(analysis_worker_count(13, available_cpus=12), 4)

    def test_worker_count_reserves_one_cpu(self) -> None:
        self.assertEqual(analysis_worker_count(13, available_cpus=4), 3)
        self.assertEqual(analysis_worker_count(2, available_cpus=2), 1)

    def test_worker_count_never_exceeds_pending_tasks(self) -> None:
        self.assertEqual(analysis_worker_count(2, available_cpus=12), 2)
        self.assertEqual(analysis_worker_count(1, available_cpus=12), 1)
        self.assertEqual(analysis_worker_count(0, available_cpus=12), 0)

    @patch.object(app_module.app.logger, "exception")
    @patch.object(app_module.multiprocessing, "get_context", return_value=None)
    @patch.object(app_module, "ProcessPoolExecutor", ImmediateExecutor)
    @patch.object(app_module, "analysis_worker_count", return_value=2)
    @patch.object(app_module, "snapshot_matches_signature", return_value=False)
    @patch.object(app_module.repository, "get_latest_trendline_analysis_snapshot")
    @patch.object(app_module, "get_trendline_analysis_signature")
    @patch.object(app_module, "save_analysis_overview_snapshot")
    @patch.object(app_module, "analyze_symbol_trendlines")
    @patch.object(app_module.repository, "list_overview_symbols")
    def test_parallel_failures_are_isolated_and_results_keep_symbol_order(
            self,
            list_symbols,
            analyze,
            save_snapshot,
            get_signature,
            get_snapshot,
            _snapshot_matches,
            _worker_count,
            _get_context,
            _log_exception,
    ) -> None:
        list_symbols.return_value = [
            {"common_symbol": "FIRST"},
            {"common_symbol": "SECOND"},
            {"common_symbol": "THIRD"},
        ]
        get_signature.side_effect = lambda symbol, **kwargs: {
            "canonical_symbol": symbol,
        }
        get_snapshot.return_value = None

        def analyze_symbol(symbol, *args):
            if symbol == "SECOND":
                raise RuntimeError("isolated failure")
            return {"symbol": symbol}

        analyze.side_effect = analyze_symbol
        save_snapshot.side_effect = lambda symbol, payload: {
            "active_count": 1,
        }

        with patch.object(
                app_module,
                "as_completed",
                side_effect=lambda futures: reversed(list(futures)),
        ):
            app_module.run_analysis_overview_refresh()

        result = app_module.analysis_overview_state["last_result"]
        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["FIRST", "SECOND", "THIRD"],
        )
        self.assertEqual(
            [item["status"] for item in result["items"]],
            ["success", "error", "success"],
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            [call.args[0] for call in save_snapshot.call_args_list],
            ["THIRD", "FIRST"],
        )


if __name__ == "__main__":
    unittest.main()
