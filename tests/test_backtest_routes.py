from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import database.db as main_db
from database import backtest_repository
import services.backtest.service as backtest_service


class BacktestRouteTests(unittest.TestCase):
    def test_recent_data_issue_repair_uses_bounded_daily_refresh(self) -> None:
        with (
            patch.object(backtest_service, "latest_completed_session_dates", return_value=["2026-08-21"]),
            patch.object(backtest_service, "validate_saved_strategy", return_value={
                "issues": [{
                    "symbol": "SPY",
                    "type": "daily",
                    "missing_date": "2026-08-10",
                    "repairable": True,
                }],
            }),
            patch.object(backtest_service, "refresh_symbol_daily_history", return_value={"updated_rows": 1}) as refresh,
        ):
            result = backtest_service.repair_saved_strategy_data(1, "spy")
        refresh.assert_called_once_with("SPY", start_date="2026-08-10")
        self.assertEqual(result["data_type"], "日线")

    def test_old_data_issue_is_not_automatically_repairable(self) -> None:
        with patch.object(backtest_service, "latest_completed_session_dates", return_value=["2026-08-21"]):
            self.assertFalse(backtest_service._is_recent_repairable_issue({
                "symbol": "SPY",
                "type": "daily",
                "missing_date": "2026-06-01",
            }))

    def test_backtest_page_uses_click_help_dialog_and_larger_selection_hitbox(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        script = (Path(app_module.app.static_folder) / "js" / "backtest.js").read_text(encoding="utf-8")
        self.assertIn('id="backtest-syntax-help-dialog"', html)
        self.assertIn('id="backtest-syntax-help-open"', html)
        self.assertIn("bt-result-select-hitbox", script)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.patcher.start()
        main_db.init_database()
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_visual_strategy_crud_validate_duplicate_and_delete(self) -> None:
        created_response = self.client.post(
            "/api/backtest/strategies",
            json={
                "name": "API测试策略",
                "design_mode": "visual",
                "selection_mode": "single",
            },
        )
        self.assertEqual(created_response.status_code, 201)
        strategy = created_response.get_json()["strategy"]

        validated = self.client.post(
            f"/api/backtest/strategies/{strategy['id']}/validate"
        )
        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.get_json()["ok"])

        strategy["name"] = "API测试策略已改名"
        updated = self.client.patch(
            f"/api/backtest/strategies/{strategy['id']}",
            json=strategy,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["strategy"]["revision"], 2)

        conflict = self.client.patch(
            f"/api/backtest/strategies/{strategy['id']}",
            json=strategy,
        )
        self.assertEqual(conflict.status_code, 409)

        duplicate = self.client.post(
            f"/api/backtest/strategies/{strategy['id']}/duplicate"
        )
        self.assertEqual(duplicate.status_code, 201)

        deleted = self.client.delete(
            f"/api/backtest/strategies/{strategy['id']}"
        )
        self.assertEqual(deleted.status_code, 200)
        listed = self.client.get("/api/backtest/strategies").get_json()
        self.assertEqual(len(listed["strategies"]), 8)
        self.assertNotIn(
            "API测试策略已改名",
            [item["name"] for item in listed["strategies"]],
        )
        reused = self.client.post(
            "/api/backtest/strategies",
            json={
                "name": "API测试策略已改名",
                "design_mode": "visual",
                "selection_mode": "single",
            },
        )
        self.assertEqual(reused.status_code, 201)

    def test_code_catalog_shipped_strategy_and_public_creation_rejected(self) -> None:
        catalog = self.client.get("/api/backtest/code-strategies")
        self.assertEqual(catalog.status_code, 200)
        item = catalog.get_json()["strategies"][0]
        self.assertEqual(item["key"], "rapid_drop_atr_rotation")
        self.assertEqual(item["version"], "1.3.0")
        self.assertEqual(item["parameter_schema"]["holdings_num"]["default"], 1)
        self.assertTrue(
            item["parameter_schema"]["enable_percent_drop_filter"]["default"]
        )

        self.assertFalse(
            item["parameter_schema"]["enable_atr_drop_filter"]["default"]
        )
        self.assertEqual(
            item["parameter_schema"]["atr_weighting"]["default"], "wilder"
        )
        self.assertEqual(
            [
                option["value"]
                for option in item["parameter_schema"]["atr_weighting"]["options"]
            ],
            ["wilder", "ema", "linear", "simple"],
        )
        sevenstar = next(
            item for item in catalog.get_json()["strategies"]
            if item["key"] == "sevenstar_etf_rotation"
        )
        self.assertEqual(sevenstar["version"], "1.1.0")
        self.assertEqual(
            sevenstar["parameter_schema"]["trend_formula_mode"]["default"],
            "consistent_w2",
        )
        self.assertEqual(
            [
                option["value"]
                for option in sevenstar["parameter_schema"]["trend_formula_mode"][
                    "options"
                ]
            ],
            ["consistent_w2", "legacy_v1"],
        )
        self.assertEqual(sevenstar["parameter_schema"]["lookback_days"]["default"], 25)
        wtme = next(
            item for item in catalog.get_json()["strategies"]
            if item["key"] == "rapid_drop_wtme_rotation"
        )
        self.assertEqual(wtme["version"], "1.1.0")
        self.assertEqual(wtme["parameter_schema"]["wtme_period"]["default"], 40)
        self.assertEqual(wtme["parameter_schema"]["wtme_half_life"]["default"], 15.0)
        self.assertEqual(wtme["parameter_schema"]["wtme_epsilon"]["default"], 1e-8)
        self.assertNotIn("atr_period", wtme["parameter_schema"])
        self.assertNotIn("atr_weighting", wtme["parameter_schema"])
        self.assertNotIn("enable_atr_drop_filter", wtme["parameter_schema"])

        listed = self.client.get("/api/backtest/strategies").get_json()["strategies"]
        sevenstar_presets = [
            item for item in listed
            if item["code_key"] == "sevenstar_etf_rotation"
        ]
        self.assertEqual(len(sevenstar_presets), 2)
        self.assertTrue(
            all(item["code_version"] == "1.1.0" for item in sevenstar_presets)
        )
        strategy = next(
            item
            for item in listed
            if item["code_key"] == "rapid_drop_atr_rotation"
        )
        self.assertEqual(strategy["name"], "急跌回避与ATR动量轮动策略")
        self.assertEqual(strategy["code_version"], "1.3.0")
        self.assertEqual(len(strategy["definition"]["symbols"]), 5)
        self.assertEqual(
            strategy["definition"]["params"]["selection_time"],
            "10:00",
        )
        self.assertEqual(
            strategy["default_settings"]["commission_per_share"],
            0.01,
        )
        self.assertEqual(strategy["default_settings"]["minimum_commission"], 1.0)
        self.assertEqual(strategy["default_settings"]["risk_free_rate"], 0.045)
        refused_delete = self.client.delete(
            f"/api/backtest/strategies/{strategy['id']}"
        )
        self.assertEqual(refused_delete.status_code, 400)
        self.assertIn(
            "代码模式策略禁止删除",
            refused_delete.get_json()["error"]["message"],
        )
        self.assertEqual(
            self.client.get(
                f"/api/backtest/strategies/{strategy['id']}"
            ).status_code,
            200,
        )
        wtme_strategy = next(
            item
            for item in listed
            if item["code_key"] == "rapid_drop_wtme_rotation"
        )
        self.assertEqual(wtme_strategy["code_version"], "1.1.0")
        self.assertEqual(wtme_strategy["definition"]["params"]["wtme_period"], 40)

        created = self.client.post(
            "/api/backtest/strategies",
            json={
                "name": "代码示例",
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": "rapid_drop_atr_rotation",
            },
        )
        self.assertEqual(created.status_code, 400)

        competition = self.client.post(
            "/api/backtest/strategies",
            json={
                "name": "默认竞争策略",
                "design_mode": "visual",
                "selection_mode": "competition",
            },
        ).get_json()["strategy"]
        self.assertEqual(
            [item["max_weight"] for item in competition["definition"]["symbols"]],
            [100, 100],
        )
        self.assertEqual(competition["definition"]["rules"], [])
        self.assertIsNone(
            competition["definition"]["competition"]["minimum_score"]
        )
        self.assertEqual(
            competition["definition"]["competition"]["eligibility_when"],
            "OPEN",
        )

    def test_run_overview_readonly_detail_and_soft_delete_routes(self) -> None:
        created = self.client.post(
            "/api/backtest/strategies",
            json={
                "name": "结果总览测试",
                "design_mode": "visual",
                "selection_mode": "single",
            },
        ).get_json()["strategy"]
        run = backtest_repository.create_run(created, created["default_settings"])
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[],
            trades=[],
            logs=[{"level": "INFO", "event_type": "TEST", "message": "日志"}],
        )
        backtest_repository.update_run(
            run["id"], status="completed", metrics={"total_return": 0.05}
        )

        overview = self.client.get("/api/backtest/runs?page=1&page_size=10")
        self.assertEqual(overview.status_code, 200)
        self.assertGreaterEqual(overview.get_json()["total_rows"], 1)
        detail = self.client.get(f"/api/backtest/runs/{run['id']}/detail")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.get_json()["run"]["strategy_snapshot"]["name"],
            "结果总览测试",
        )
        refused = self.client.post(
            "/api/backtest/runs/deletions", json={"run_ids": [run["id"]]}
        )
        self.assertEqual(refused.status_code, 400)
        cleaned = self.client.post(
            "/api/backtest/runs/deletions",
            json={"run_ids": [run["id"]], "confirm": True},
        )
        self.assertEqual(cleaned.status_code, 200)
        self.assertEqual(cleaned.get_json()["deleted_log_rows"], 1)
        after = self.client.get("/api/backtest/runs?page=1&page_size=10")
        self.assertEqual(after.get_json()["total_rows"], 0)
        self.assertEqual(
            self.client.get(f"/api/backtest/runs/{run['id']}/detail").status_code,
            400,
        )

    @patch.object(app_module, "build_run_xls", return_value=b"excel-data")
    def test_xls_log_download_uses_excel_attachment(self, build_xls) -> None:
        response = self.client.get("/api/backtest/runs/42/logs.xls")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"excel-data")
        self.assertEqual(response.mimetype, "application/vnd.ms-excel")
        self.assertIn("backtest-42.xls", response.headers["Content-Disposition"])
        build_xls.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
