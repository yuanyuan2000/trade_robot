from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import database.db as main_db


class BacktestRouteTests(unittest.TestCase):
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
        self.assertEqual(len(listed["strategies"]), 5)
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

        listed = self.client.get("/api/backtest/strategies").get_json()["strategies"]
        strategy = next(
            item
            for item in listed
            if item["code_key"] == "rapid_drop_atr_rotation"
        )
        self.assertEqual(strategy["name"], "急跌回避与ATR动量轮动策略")
        self.assertEqual(strategy["code_version"], "1.0.0")
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


if __name__ == "__main__":
    unittest.main()
