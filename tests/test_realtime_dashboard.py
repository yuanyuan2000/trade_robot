from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import database.db as main_db
from database import backtest_repository, realtime_repository, repository
from services.backtest.presets import ensure_shipped_strategy_presets
from services.market_overview_coordinator import MarketOverviewRefreshCoordinator
from services.realtime_dashboard_service import (
    build_realtime_dashboard,
    clear_realtime_dashboard_cache,
    dashboard_recommendations,
)
from services.realtime_market_data import IEXMarketDataHub
from services.realtime_decision_service import RealtimeDecisionEvaluator
from services.realtime_panel_script import generate_panel_settings, validate_panel_script
from services.realtime_presets import ensure_shipped_realtime_tasks


class RealtimeDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.db_patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.data_patcher = patch.object(main_db, "DATA_DIR", self.data_dir)
        self.db_patcher.start()
        self.data_patcher.start()
        main_db.init_database()
        ensure_shipped_strategy_presets()
        ensure_shipped_realtime_tasks()
        self._seed_overview_rows(("SPY", "GLD", "OUTSIDE"))

    def tearDown(self) -> None:
        self.data_patcher.stop()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def _seed_overview_rows(self, symbols: tuple[str, ...]) -> None:
        start = date(2026, 4, 1)
        for symbol_index, symbol in enumerate(symbols):
            repository.upsert_symbol(symbol, {"name": f"{symbol} name"})
            repository.set_symbol_overview_visibility(symbol, True)
            rows = []
            for index in range(80):
                close = 80 + symbol_index * 10 + index * (0.35 + symbol_index * 0.03)
                rows.append({
                    "date": (start + timedelta(days=index)).isoformat(),
                    "open": close - 0.2,
                    "high": close + 0.8,
                    "low": close - 0.8,
                    "close": close,
                    "volume": 1_000_000 + index * 1000,
                    "price_basis": "raw",
                })
            repository.upsert_daily_prices(symbol, rows, source_provider="alpaca", source_timeframe="1Day")

    def test_dashboard_uses_overview_universe_and_marks_task_candidates(self) -> None:
        task = next(
            task for task in realtime_repository.list_tasks()
            if task["strategy_snapshot"].get("code_key") == "sevenstar_etf_rotation"
        )
        with patch.object(IEXMarketDataHub, "event_snapshot", side_effect=AssertionError("must not call external API")):
            payload = build_realtime_dashboard(task["id"], force=True)
        rows = {row["symbol"]: row for row in payload["rows"]}
        self.assertEqual(set(rows), {"SPY", "GLD", "OUTSIDE"})
        self.assertTrue(rows["SPY"]["is_candidate"])
        self.assertTrue(rows["GLD"]["is_candidate"])
        self.assertFalse(rows["OUTSIDE"]["is_candidate"])
        self.assertEqual(rows["SPY"]["price_source"], "alpaca")
        self.assertEqual(rows["SPY"]["price_timeframe"], "1Day")
        self.assertEqual(rows["SPY"]["price_basis"], "raw")
        self.assertEqual(payload["source"], "market_overview_database")
        self.assertFalse(payload["external_api_called"])
        self.assertEqual(payload["observation_mode"], "strategy_latest_simulation")
        self.assertFalse(payload["formal_decision"])
        self.assertIn("不是正式决策", payload["observation_note"])
        self.assertEqual(payload["default_sort"], {"key": "score", "direction": "desc"})
        recommendations = dashboard_recommendations(payload)
        self.assertLessEqual(len(recommendations), 3)
        self.assertEqual(
            [item["rank"] for item in recommendations],
            list(range(1, len(recommendations) + 1)),
        )

    def test_running_dashboard_candidate_marker_uses_run_snapshot(self) -> None:
        task = next(
            task for task in realtime_repository.list_tasks()
            if task["strategy_snapshot"].get("code_key") == "sevenstar_etf_rotation"
        )
        run = realtime_repository.create_run(task)
        changed = {**task["strategy_snapshot"], "definition": {
            **task["strategy_snapshot"]["definition"],
            "symbols": [
                {**item, "symbol": "OUTSIDE" if item["symbol"] == "SPY" else item["symbol"]}
                for item in task["strategy_snapshot"]["definition"]["symbols"]
            ],
        }}
        realtime_repository.update_task(task["id"], strategy_snapshot=changed)
        realtime_repository.set_task_runtime(task["id"], runtime_state="running")
        clear_realtime_dashboard_cache(task["id"])
        payload = build_realtime_dashboard(task["id"], force=True)
        rows = {row["symbol"]: row for row in payload["rows"]}
        self.assertEqual(payload["candidate_snapshot_source"], "run_snapshot")
        self.assertTrue(rows["SPY"]["is_candidate"])
        self.assertFalse(rows["OUTSIDE"]["is_candidate"])

    def test_card_recommendations_keep_only_top_three_eligible_rows(self) -> None:
        dashboard = {
            "selection_mode": "competition",
            "rows": [
                {"symbol": "FOUR", "eligible": True, "rank": 4, "score": 4},
                {"symbol": "TWO", "eligible": True, "rank": 2, "score": 8},
                {"symbol": "FILTERED", "eligible": False, "rank": 1, "score": 99},
                {"symbol": "ONE", "eligible": True, "rank": 1, "score": 9},
                {"symbol": "THREE", "eligible": True, "rank": 3, "score": 7},
            ],
        }
        recommendations = dashboard_recommendations(dashboard)
        self.assertEqual(
            [item["symbol"] for item in recommendations],
            ["ONE", "TWO", "THREE"],
        )
        self.assertEqual(
            dashboard_recommendations({**dashboard, "selection_mode": "single"}),
            [],
        )

    def test_sevenstar_dashboard_reuses_production_metrics_and_ranks_panel_only(self) -> None:
        task = next(task for task in realtime_repository.list_tasks() if task["strategy_snapshot"].get("code_key") == "sevenstar_etf_rotation")
        payload = build_realtime_dashboard(task["id"], force=True)
        params = task["strategy_snapshot"]["definition"]["params"]
        help_by_key = {column["key"]: column["help"] for column in payload["columns"]}
        label_by_key = {column["key"]: column["label"] for column in payload["columns"]}
        self.assertTrue(all("策略" in label for label in label_by_key.values()))
        self.assertIn(f'{params["lookback_days"] + 1} 个点', help_by_key["annualized_returns"])
        self.assertIn(f'第 {params["short_lookback_days"]} 个完整交易日', help_by_key["short_annualized"])
        self.assertIn(f'{params["min_score_threshold"]:g}', help_by_key["score"])
        available = [row for row in payload["rows"] if row["status"] != "不可计算"]
        self.assertTrue(available)
        for row in available:
            self.assertAlmostEqual(
                row["metrics"]["score"],
                row["metrics"]["annualized_returns"] * row["metrics"]["r_squared"],
                places=10,
            )
        ranked = [row for row in available if row["rank"] is not None]
        self.assertEqual([row["rank"] for row in sorted(ranked, key=lambda row: row["rank"])], list(range(1, len(ranked) + 1)))

    def test_atr_and_wtme_dashboard_components_match_score_formulas(self) -> None:
        strategies = {
            item.get("code_key"): item for item in backtest_repository.list_strategies()
            if item.get("code_key") in {"rapid_drop_atr_rotation", "rapid_drop_wtme_rotation"}
        }
        for code_key, strategy in strategies.items():
            task = realtime_repository.create_task(
                name=f"dashboard {code_key}", strategy=strategy,
                follow_strategy=True, settings=strategy["default_settings"],
                notification_settings={"enabled": False},
                portfolio_state={"cash": 100000, "positions": {}},
                panel_settings=generate_panel_settings(strategy),
            )
            clear_realtime_dashboard_cache(task["id"])
            payload = build_realtime_dashboard(task["id"], force=True)
            params = strategy["definition"]["params"]
            help_by_key = {column["key"]: column["help"] for column in payload["columns"]}
            label_by_key = {column["key"]: column["label"] for column in payload["columns"]}
            row = next(item for item in payload["rows"] if item["symbol"] == "SPY")
            self.assertNotEqual(row["status"], "不可计算")
            if code_key == "rapid_drop_atr_rotation":
                self.assertEqual(label_by_key["score"], "策略 ATR 评分")
                self.assertIn(f'{params["momentum_lookback_sessions"]} 日价格位移', help_by_key["score"])
                self.assertIn(f'{params["atr_period"]} 日策略 ATR', help_by_key["score"])
                self.assertAlmostEqual(
                    row["metrics"]["score"],
                    row["metrics"]["price_displacement"] / row["metrics"]["atr"],
                    places=10,
                )
            else:
                self.assertEqual(label_by_key["score"], "策略 WTME 评分")
                self.assertIn(f'最近 {params["wtme_period"]} 个收益观测', help_by_key["weighted_return"])
                self.assertIn(f'{params["wtme_half_life"]:g} 个交易日', help_by_key["weighted_return"])
                epsilon = strategy["definition"]["params"]["wtme_epsilon"]
                self.assertAlmostEqual(
                    row["metrics"]["score"],
                    100 * row["metrics"]["weighted_return"]
                    / (row["metrics"]["weighted_true_range"] + epsilon),
                    places=10,
                )

    def test_visual_panel_script_is_generated_for_all_selection_modes(self) -> None:
        strategies = backtest_repository.list_strategies()
        visual_modes = {strategy["selection_mode"]: strategy for strategy in strategies if strategy["design_mode"] == "visual"}
        self.assertEqual(set(visual_modes), {"single", "distribution", "competition"})
        for strategy in visual_modes.values():
            settings = generate_panel_settings(strategy)
            parsed = validate_panel_script(settings["script"])
            self.assertLessEqual(len(parsed["columns"]), 12)
            if strategy["selection_mode"] == "competition":
                self.assertIn("score", {column["key"] for column in parsed["columns"]})

    def test_visual_task_creation_stores_independent_panel_revision(self) -> None:
        strategy = next(item for item in backtest_repository.list_strategies() if item["design_mode"] == "visual")
        panel = generate_panel_settings(strategy)
        task = realtime_repository.create_task(
            name="visual panel revision",
            strategy=strategy,
            follow_strategy=True,
            settings=strategy["default_settings"],
            notification_settings={"enabled": False},
            portfolio_state={"cash": 100000, "positions": {}},
            panel_settings=panel,
        )
        updated = realtime_repository.update_panel_settings(
            task["id"], {**panel, "customized": True},
            expected_panel_revision=task["panel_revision"],
        )
        self.assertEqual(updated["revision"], task["revision"])
        self.assertEqual(updated["panel_revision"], task["panel_revision"] + 1)

    def test_sevenstar_seed_is_idempotent_and_soft_delete_does_not_resurrect(self) -> None:
        first = ensure_shipped_realtime_tasks()
        second = ensure_shipped_realtime_tasks()
        self.assertEqual(first["id"], second["id"])
        realtime_repository.soft_delete_task(first["id"])
        result = ensure_shipped_realtime_tasks()
        self.assertEqual(result["id"], first["id"])
        self.assertIsNotNone(result["deleted_at"])
        self.assertFalse(any(task["id"] == first["id"] for task in realtime_repository.list_tasks()))

    def test_sevenstar_presets_share_implementation_version_and_parameters(self) -> None:
        presets = [
            item for item in backtest_repository.list_strategies()
            if item.get("code_key") == "sevenstar_etf_rotation"
        ]
        self.assertEqual(len(presets), 2)
        small, large = sorted(presets, key=lambda item: len(item["definition"]["symbols"]))
        self.assertEqual(small["code_key"], large["code_key"])
        self.assertEqual(small["code_version"], large["code_version"])
        self.assertEqual(small["definition"]["params"], large["definition"]["params"])
        self.assertNotEqual(small["definition"]["symbols"], large["definition"]["symbols"])

    def test_iex_hub_start_does_not_begin_background_latest_polling(self) -> None:
        hub = IEXMarketDataHub(poll_seconds=5)
        with patch("services.realtime_market_data.fetch_latest_stock_bars") as latest:
            hub.set_symbols(["SPY"])
            hub.start()
            hub.stop()
        latest.assert_not_called()

    def test_formal_event_snapshot_remains_independent_when_overview_auto_is_off(self) -> None:
        repository.set_system_setting("market_overview_auto_refresh_enabled", False)
        strategy = next(item for item in backtest_repository.list_strategies() if item["design_mode"] == "visual" and item["selection_mode"] == "single")
        task = realtime_repository.create_task(
            name="formal event independent", strategy=strategy,
            follow_strategy=False, settings=strategy["default_settings"],
            notification_settings={"enabled": False},
            portfolio_state={"cash": 100000, "positions": {}},
            panel_settings=generate_panel_settings(strategy),
        )
        run = realtime_repository.create_run(task)
        latest = repository.get_daily_prices("SPY")[-1]
        hub = Mock()
        hub.event_snapshot.return_value = {
            "symbols": {"SPY": {
                "signal_price": latest["close"], "fill_price": latest["close"],
                "signal_time": "2026-08-12T13:29:00Z", "fill_time": "2026-08-12T13:30:00Z",
                "daily": latest, "daily_is_complete": False, "latest_minute": latest,
                "cumulative_volume": None, "source": "alpaca", "feed": "iex",
            }},
            "source": "alpaca", "feed": "iex", "missing": [],
            "requested_at": "2026-08-12T13:30:00Z",
        }
        RealtimeDecisionEvaluator(hub).evaluate(task, run, trading_date="2026-08-12", event="OPEN")
        hub.event_snapshot.assert_called_once()

    def test_dashboard_routes_and_default_ui_are_available(self) -> None:
        import app as app_module
        task = next(task for task in realtime_repository.list_tasks() if task["strategy_snapshot"].get("code_key") == "sevenstar_etf_rotation")
        client = app_module.app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertIn('data-realtime-panel="dashboard"', html)
        self.assertIn('data-realtime-tab="parameters"', html)
        self.assertNotIn('class="realtime-dashboard-summary"', html)
        self.assertIn('class="realtime-back-icon"', html)
        self.assertIn("按任务策略模拟最新时点，非正式决策", html)
        self.assertIn("IEX 与 SIP 数据源暂不统一", html)
        script = (Path(__file__).parents[1] / "static" / "js" / "realtime.js").read_text(encoding="utf-8")
        self.assertNotIn("任务快照候选池 · 每 60 秒轮询内部数据", script)
        self.assertIn('`已过滤 ${summary.filtered ?? 0}`', script)
        self.assertIn('market-overview-auto-refresh-changed', script)
        self.assertIn('data-rt-open-symbol', script)
        self.assertIn('returnContext: "realtime-dashboard"', script)
        app_script = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('function applyOverviewAutoRefreshState', app_script)
        self.assertIn('market-overview-auto-refresh-changed', app_script)
        self.assertIn('document.addEventListener("open-market-detail"', app_script)
        self.assertIn('document.dispatchEvent(new CustomEvent("return-to-realtime-dashboard"))', app_script)
        styles = (Path(__file__).parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".realtime-details-cell", styles)
        self.assertIn(".realtime-details-dialog", styles)
        self.assertIn("max-width: min(960px, calc(100vw - 32px))", styles)
        self.assertIn("max-height: calc(100vh - 130px)", styles)
        response = client.get(f"/api/realtime/tasks/{task['id']}/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["dashboard"]["external_api_called"])
        task_list = client.get("/api/realtime/tasks")
        self.assertEqual(task_list.status_code, 200)
        listed = next(item for item in task_list.get_json()["tasks"] if item["id"] == task["id"])
        self.assertIn("overview_recommendations", listed)
        self.assertLessEqual(len(listed["overview_recommendations"]), 3)
        validation = client.post(f"/api/realtime/tasks/{task['id']}/validate")
        self.assertEqual(validation.status_code, 200)
        self.assertTrue(validation.get_json()["events"])

    def test_visual_panel_routes_work_independently_while_task_is_running(self) -> None:
        import app as app_module
        strategy = next(item for item in backtest_repository.list_strategies() if item["design_mode"] == "visual")
        client = app_module.app.test_client()
        created = client.post("/api/realtime/tasks", json={
            "strategy_id": strategy["id"], "name": "visual route task",
            "follow_strategy": True,
        })
        self.assertEqual(created.status_code, 201)
        task = created.get_json()["task"]
        self.assertTrue(task["panel_settings"]["script"])
        realtime_repository.set_task_runtime(task["id"], runtime_state="running")
        validated = client.post(
            f"/api/realtime/tasks/{task['id']}/panel/validate",
            json={"script": task["panel_settings"]["script"]},
        )
        self.assertEqual(validated.status_code, 200)
        saved = client.patch(
            f"/api/realtime/tasks/{task['id']}/panel",
            json={
                "panel_revision": task["panel_revision"],
                "script": task["panel_settings"]["script"],
            },
        )
        self.assertEqual(saved.status_code, 200)
        updated = saved.get_json()["task"]
        self.assertEqual(updated["revision"], task["revision"])
        self.assertGreater(updated["panel_revision"], task["panel_revision"])
        logs = client.get(f"/api/realtime/tasks/{task['id']}/logs?kind=decision")
        self.assertEqual(logs.status_code, 200)

    def test_browser_has_no_market_overview_external_refresh_timer(self) -> None:
        script = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("overviewLiveRefreshMs", script)
        self.assertNotIn("refreshOverviewLivePrices", script)

    def test_overview_coordinator_persists_one_shared_switch(self) -> None:
        coordinator = MarketOverviewRefreshCoordinator(sync_callback=lambda: {"updated_rows": 0})
        state = coordinator.set_auto_enabled(False)
        self.assertFalse(state["auto_enabled"])
        second = MarketOverviewRefreshCoordinator(sync_callback=lambda: {})
        second.start()
        try:
            self.assertFalse(second.snapshot()["auto_enabled"])
        finally:
            second.stop()


if __name__ == "__main__":
    unittest.main()
