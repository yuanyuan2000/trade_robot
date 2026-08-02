from __future__ import annotations

import unittest
from unittest.mock import patch

import app as app_module


class MarketDataRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()

    @patch.object(app_module, "get_market_data")
    def test_query_can_request_intraday_initialization(self, get_market_data) -> None:
        get_market_data.return_value = {"ok": True, "data": []}

        response = self.client.get(
            "/api/market-data?symbol=GLD&include_intraday=1"
        )

        self.assertEqual(response.status_code, 200)
        get_market_data.assert_called_once_with(
            "GLD",
            include_intraday=True,
        )

    @patch.object(app_module, "update_full_market_data")
    def test_regular_update_without_checkbox_only_updates_daily(self, update) -> None:
        update.return_value = {"ok": True, "data": []}

        response = self.client.post(
            "/api/market-data/update",
            json={"symbol": "GLD"},
        )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(
            "GLD",
            initialize_intraday=False,
        )

    @patch.object(app_module, "update_full_market_data")
    def test_regular_update_with_checkbox_updates_intraday(self, update) -> None:
        update.return_value = {"ok": True, "data": []}

        response = self.client.post(
            "/api/market-data/update",
            json={"symbol": "GLD", "include_intraday": True},
        )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(
            "GLD",
            initialize_intraday=True,
        )

    @patch.object(app_module, "start_market_data_update")
    def test_background_update_returns_pollable_job(self, start_update) -> None:
        start_update.return_value = (
            {
                "id": "job-1", "symbol": "GLD", "running": True,
                "progress": 0.25, "current_date": "2024-01-02",
            },
            True,
        )

        response = self.client.post(
            "/api/market-data/update",
            json={
                "symbol": "GLD",
                "include_intraday": True,
                "background": True,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job"]["id"], "job-1")
        start_update.assert_called_once_with(
            "GLD", include_intraday=True, query_only=False
        )

    @patch.object(app_module, "start_market_data_update")
    def test_first_query_can_run_in_background_with_progress(self, start_update) -> None:
        start_update.return_value = (
            {
                "id": "query-job", "symbol": "BTC/USD", "running": True,
                "progress": 0.1, "current_date": "2021-01-01",
            },
            True,
        )

        response = self.client.post(
            "/api/market-data/update",
            json={
                "symbol": "BTC/USD",
                "include_intraday": True,
                "background": True,
                "query_only": True,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job"]["id"], "query-job")
        start_update.assert_called_once_with(
            "BTC/USD", include_intraday=True, query_only=True
        )

    def test_duplicate_symbol_reuses_and_upgrades_running_job(self) -> None:
        job = {
            "id": "dedupe", "symbol": "BTC/USD", "running": True,
            "include_intraday": False, "query_only": True,
            "message": "正在更新日线", "updated_at": "old",
        }
        with app_module.market_data_update_lock:
            app_module.market_data_update_jobs["dedupe"] = job
        try:
            reused, started = app_module.start_market_data_update(
                "BTC/USD", include_intraday=True, query_only=True
            )
            self.assertFalse(started)
            self.assertEqual(reused["id"], "dedupe")
            self.assertTrue(reused["include_intraday"])
        finally:
            with app_module.market_data_update_lock:
                app_module.market_data_update_jobs.pop("dedupe", None)

    @patch.object(app_module, "get_market_data")
    def test_running_daily_query_finishes_late_intraday_upgrade(self, get_data) -> None:
        def execute(_symbol, *, include_intraday, progress_callback):
            progress_callback({"stage": "daily", "progress": 0.5})
            if not include_intraday:
                with app_module.market_data_update_lock:
                    app_module.market_data_update_jobs["upgrade"][
                        "include_intraday"
                    ] = True
            return {"ok": True, "data": [{"date": "2026-07-31"}]}

        get_data.side_effect = execute
        with app_module.market_data_update_lock:
            app_module.market_data_update_jobs["upgrade"] = {
                "id": "upgrade", "symbol": "BTC/USD", "running": True,
                "include_intraday": False, "query_only": True,
                "stage": "queued", "progress": 0.0, "current_date": None,
                "pages": 0, "rows": 0, "message": "queued",
                "result": None, "error": None, "updated_at": "old",
            }
        try:
            app_module.run_market_data_update("upgrade", "BTC/USD")
            job = app_module.market_data_update_jobs["upgrade"]
            self.assertFalse(job["running"])
            self.assertEqual(job["stage"], "completed")
            self.assertEqual(
                [call.kwargs["include_intraday"] for call in get_data.call_args_list],
                [False, True],
            )
        finally:
            with app_module.market_data_update_lock:
                app_module.market_data_update_jobs.pop("upgrade", None)

    def test_background_update_status_returns_progress(self) -> None:
        with app_module.market_data_update_lock:
            app_module.market_data_update_jobs["status-test"] = {
                "id": "status-test", "symbol": "GLD", "running": True,
                "progress": 0.5, "current_date": "2024-06-30",
                "message": "分钟数据已更新至 2024-06-30",
            }
        try:
            response = self.client.get(
                "/api/market-data/update-status/status-test"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["job"]["progress"], 0.5)
            self.assertEqual(
                response.get_json()["job"]["current_date"],
                "2024-06-30",
            )
        finally:
            with app_module.market_data_update_lock:
                app_module.market_data_update_jobs.pop("status-test", None)


if __name__ == "__main__":
    unittest.main()
