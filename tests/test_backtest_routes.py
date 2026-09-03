from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from services.backtest.code_strategies import RapidDropWtmeRotationStrategy
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
        styles = (Path(app_module.app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn('id="backtest-syntax-help-dialog"', html)
        self.assertIn('id="backtest-syntax-help-open"', html)
        self.assertIn("r_square(25)", html)
        self.assertIn("bt-result-select-hitbox", script)
        self.assertIn(
            'BT_PREVIOUS_CLOSE_INTRADAY_SYMBOLS = new Set(["USDINDEX", "US10Y"])',
            script,
        )
        self.assertIn("历史回测需要分钟级价格时，临时使用上一交易日收盘价", script)
        overview_renderer = script[
            script.index("async function loadBacktestResultsOverview"):
            script.index("async function openBacktestRunDetail")
        ]
        self.assertIn('"买入数"', overview_renderer)
        self.assertIn('"杠杆率"', overview_renderer)
        self.assertIn("btDateOnly(run.created_at)", overview_renderer)
        self.assertNotIn("trade_count", overview_renderer)
        self.assertIn('" / <wbr>"', script)
        self.assertIn("applyBacktestResultsSort", script)
        self.assertIn('resultsSort: { key: "id", direction: "desc" }', script)
        self.assertIn('id="backtest-results-page-size"', html)
        for size in (20, 50, 100, 200):
            self.assertIn(f'<option value="{size}">{size}条</option>', html)
        self.assertIn(".backtest-result-symbol { white-space: nowrap; }", styles)
        self.assertIn(".backtest-symbol-tooltip-row { display: contents; }", styles)
        self.assertIn(".backtest-syntax-help-dialog { width: min(760px, calc(100vw - 32px)); overflow: hidden; }", styles)
        self.assertIn(".backtest-syntax-help-content { min-height: 0; overflow: auto; }", styles)

    def test_backtest_page_exposes_detailed_analysis_workspace(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        analysis_script = (
            Path(app_module.app.static_folder) / "js" / "backtest_analysis.js"
        ).read_text(encoding="utf-8")

        self.assertIn('id="backtest-analysis-open"', html)
        self.assertIn('id="backtest-analysis-page"', html)
        self.assertIn('id="backtest-analysis-candles"', html)
        self.assertIn('id="backtest-analysis-leverage"', html)
        self.assertIn('id="backtest-dynamic-leverage-enabled"', html)
        self.assertIn('id="backtest-dynamic-volatility-period"', html)
        self.assertIn('id="backtest-dynamic-stress-days"', html)
        self.assertIn('id="backtest-dynamic-max-loss"', html)
        self.assertIn('id="backtest-dynamic-max-leverage"', html)
        self.assertIn('id="backtest-dynamic-rebalance-on-change"', html)
        self.assertIn('data-months="12"', html)
        self.assertIn("backtest-analysis-progress", analysis_script)
        self.assertIn("/analysis/decision", analysis_script)
        self.assertIn("backtest-analysis-pnl-positive", analysis_script)
        self.assertIn("backtest-analysis-pnl-negative", analysis_script)
        self.assertIn('item.type === "strategy" ? 3.0', analysis_script)
        self.assertIn('data-series-type="${btEscape(item.type)}"', analysis_script)
        self.assertIn("leveragedBenchmarks", analysis_script)
        self.assertIn("<span>持仓</span><span>评分</span>", analysis_script)

    def test_backtest_analysis_routes_forward_range_and_date_parameters(self) -> None:
        snapshot = {"run": {"id": 19}}
        with (
            patch.object(
                app_module.backtest_run_manager,
                "analysis_snapshot",
                return_value=snapshot,
            ) as snapshot_reader,
            patch.object(app_module, "build_analysis", return_value={"series": []}) as builder,
        ):
            response = self.client.get(
                "/api/backtest/runs/19/analysis?start_date=2024-01-01&end_date=2024-03-31"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["series"], [])
        snapshot_reader.assert_called_once_with(19)
        builder.assert_called_once_with(snapshot, "2024-01-01", "2024-03-31")

        with (
            patch.object(
                app_module.backtest_run_manager,
                "analysis_snapshot",
                return_value=snapshot,
            ) as snapshot_reader,
            patch.object(
                app_module,
                "build_backtest_analysis_decision",
                return_value={"mode": "competition", "rows": []},
            ),
        ):
            response = self.client.get(
                "/api/backtest/runs/19/analysis/decision?date=2024-03-29"
            )

        self.assertEqual(response.status_code, 200)
        snapshot_reader.assert_called_once_with(19, trading_date="2024-03-29")

    def test_backtest_metric_panel_uses_live_retained_fields_only(self) -> None:
        script = (Path(app_module.app.static_folder) / "js" / "backtest.js").read_text(encoding="utf-8")
        start = script.index("function renderBacktestMetrics")
        end = script.index("function renderBacktestChart", start)
        renderer = script[start:end]

        for label in ("运行结果", "总收益率", "年化收益率", "最大回撤", "夏普率", "Sortino", "交易次数", "胜率"):
            self.assertIn(f'["{label}"', renderer)
        for label in ("整体杠杆倍率", "期末权益", "累计手续费", "滑点成本", "换手率", "超额收益"):
            self.assertNotIn(f'["{label}"', renderer)
        self.assertIn('? isRunning ? "运行中" : "—"', renderer)
        self.assertIn("renderBacktestMetrics(payload.run?.metrics, payload.run)", script)

    def test_backtest_returns_use_directional_colors(self) -> None:
        script = (Path(app_module.app.static_folder) / "js" / "backtest.js").read_text(encoding="utf-8")
        stylesheet = (Path(app_module.app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn('value > 0 ? "backtest-return-positive" : "backtest-return-negative"', script)
        self.assertIn('returnClass("total_return")', script)
        self.assertIn('returnClass("annualized_return")', script)
        self.assertIn(".backtest-metric strong.backtest-return-positive", stylesheet)
        self.assertIn("color: var(--success);", stylesheet)
        self.assertIn(".backtest-metric strong.backtest-return-negative", stylesheet)
        self.assertIn("color: var(--danger);", stylesheet)

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
        self.assertEqual(strategy["market"], {
            "type": "US_EQUITY",
            "calendar": "XNYS",
            "timezone": "America/New_York",
        })

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
        self.assertEqual(
            updated.get_json()["strategy"]["market"]["type"],
            "US_EQUITY",
        )

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
        self.assertEqual(sevenstar["version"], "1.2.0")
        self.assertNotIn("minimum_trade_value_usd", sevenstar["parameter_schema"])
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
        self.assertEqual(wtme["version"], "2.0.0")
        self.assertFalse(
            wtme["parameter_schema"]["enable_upside_sell_protection"]["default"]
        )
        wtme_params = wtme["parameter_schema"]
        self.assertLess(
            wtme["parameter_order"].index("buy_top_n"),
            wtme["parameter_order"].index("buy_score_threshold"),
        )
        self.assertEqual(wtme_params["buy_condition_operator"]["default"], "or")
        self.assertEqual(
            wtme_params["buy_score_threshold"]["inline_prefix_parameter"],
            "buy_condition_operator",
        )
        self.assertEqual(
            wtme["parameter_schema"]["max_simultaneous_holdings"]["default"],
            1,
        )
        self.assertEqual(wtme["parameter_schema"]["buy_top_n"]["default"], 1)
        self.assertEqual(
            wtme["parameter_schema"]["buy_score_threshold"]["default"],
            9999.0,
        )
        self.assertEqual(wtme["parameter_schema"]["allocation_mode"]["default"], "equal")
        self.assertEqual(
            [
                option["value"]
                for option in wtme["parameter_schema"]["allocation_mode"]["options"]
            ],
            [
                "equal",
                "linear_rank",
                "leveraged_equal",
                "leveraged_linear_rank",
            ],
        )
        self.assertEqual(wtme["parameter_schema"]["wtme_period"]["default"], 40)
        self.assertEqual(wtme["parameter_schema"]["wtme_half_life"]["default"], 15.0)
        self.assertEqual(wtme["parameter_schema"]["wtme_epsilon"]["default"], 1e-8)
        for retired in RapidDropWtmeRotationStrategy.retired_parameters:
            self.assertNotIn(retired, wtme["parameter_schema"])
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
            all(item["code_version"] == "1.2.0" for item in sevenstar_presets)
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
        self.assertEqual(wtme_strategy["code_version"], "2.0.0")
        self.assertFalse(
            wtme_strategy["definition"]["params"]["enable_upside_sell_protection"]
        )
        self.assertEqual(
            wtme_strategy["definition"]["params"]["max_simultaneous_holdings"],
            1,
        )
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

    def test_run_overview_readonly_detail_and_hard_delete_routes(self) -> None:
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
        overview_item = next(
            item for item in overview.get_json()["items"] if item["id"] == run["id"]
        )
        self.assertEqual(
            overview_item["symbol_leverages"],
            [{"symbol": "SPY", "leverage_multiplier": 1.0}],
        )
        self.assertEqual(overview_item["max_buy_count"], 1)
        self.assertEqual(overview_item["leverage_display"], "1x")
        self.assertEqual(
            self.client.get("/api/backtest/runs?page=1&page_size=200").get_json()["page_size"],
            200,
        )
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
        self.assertEqual(cleaned.get_json()["deleted_run_rows"], 1)
        self.assertEqual(cleaned.get_json()["deleted_trade_rows"], 0)
        after = self.client.get("/api/backtest/runs?page=1&page_size=10")
        self.assertEqual(after.get_json()["total_rows"], 0)
        self.assertEqual(
            self.client.get(f"/api/backtest/runs/{run['id']}/detail").status_code,
            400,
        )

    def test_run_overview_uses_historical_wtme_and_missing_snapshot_metadata(self) -> None:
        wtme = backtest_service.create_default_strategy(
            name="WTME 总览快照测试",
            design_mode="code",
            selection_mode="competition",
            code_key="rapid_drop_wtme_rotation",
        )
        wtme["definition"]["params"]["allocation_mode"] = "leveraged_linear_rank"
        wtme["definition"]["params"]["buy_top_n"] = 3
        wtme["definition"]["params"]["max_simultaneous_holdings"] = 2
        wtme["definition"]["symbols"][0]["leverage_multiplier"] = 3.0
        wtme_settings = {**wtme["default_settings"], "leverage_multiplier": 1.5}
        wtme_run = backtest_repository.create_run(wtme, wtme_settings)

        visual = backtest_service.create_default_strategy(
            name="旧快照缺失字段测试",
            design_mode="visual",
            selection_mode="distribution",
        )
        visual["definition"]["symbols"][0].pop("leverage_multiplier")
        visual_settings = dict(visual["default_settings"])
        visual_settings.pop("leverage_multiplier")
        visual_run = backtest_repository.create_run(visual, visual_settings)

        response = self.client.get("/api/backtest/runs?page=1&page_size=10")
        self.assertEqual(response.status_code, 200)
        items = {item["id"]: item for item in response.get_json()["items"]}

        wtme_item = items[wtme_run["id"]]
        self.assertEqual(wtme_item["max_buy_count"], 2)
        self.assertEqual(wtme_item["leverage_display"], "1.5x持仓数")
        self.assertEqual(
            wtme_item["symbol_leverages"][0],
            {"symbol": "SPY", "leverage_multiplier": 3.0},
        )

        visual_item = items[visual_run["id"]]
        self.assertEqual(visual_item["max_buy_count"], 2)
        self.assertEqual(visual_item["leverage_display"], "-")
        self.assertIsNone(visual_item["symbol_leverages"][0]["leverage_multiplier"])

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
