from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import database.db as main_db
from database import realtime_repository
from services.backtest.service import create_default_strategy
from services.realtime_mail import render_message
from services.realtime_scheduler import RealtimeTaskManager
from services.realtime_market_data import IEXMarketDataHub


class RealtimeDecisionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.db_patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.data_patcher = patch.object(main_db, "DATA_DIR", self.data_dir)
        self.db_patcher.start()
        self.data_patcher.start()
        main_db.init_database()

    def tearDown(self) -> None:
        self.data_patcher.stop()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_normal_notification_slot_is_atomic_and_exactly_one_minute(self) -> None:
        strategy = create_default_strategy(name="限频依赖策略", design_mode="visual", selection_mode="single")
        task = realtime_repository.create_task(
            name="限频测试",
            strategy=strategy,
            follow_strategy=False,
            settings={},
            notification_settings={},
            portfolio_state={},
        )
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        first = realtime_repository.reserve_normal_send(task["id"], now=now, cooldown_seconds=60)
        second = realtime_repository.reserve_normal_send(task["id"], now=now, cooldown_seconds=60)
        self.assertTrue(first)
        self.assertFalse(second)
        third = realtime_repository.reserve_normal_send(task["id"], now=now.replace(minute=1), cooldown_seconds=60)
        self.assertTrue(third)

    def test_open_signal_uses_previous_completed_daily_close(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(symbol, *, timeframe, start=None, end=None, feed="iex", limit=1000, max_pages=1):
            if timeframe == "1Day":
                return {
                    "data": [
                        {"timestamp": "2026-07-31T00:00:00Z", "open": 98, "high": 101, "low": 97, "close": 100, "volume": 10},
                        {"timestamp": "2026-08-03T00:00:00Z", "open": 103, "high": 104, "low": 102, "close": 103, "volume": 2},
                    ]
                }
            return {
                "data": [
                    {"timestamp": "2026-08-03T13:30:00Z", "open": 103.5, "high": 104, "low": 103, "close": 103.8, "volume": 4},
                ]
            }

        with patch("services.realtime_market_data.fetch_stock_bars", side_effect=fake_bars):
            snapshot = hub.event_snapshot(["SPY"], trading_date="2026-08-03", event="OPEN")
        row = snapshot["symbols"]["SPY"]
        self.assertEqual(row["signal_price"], 100.0)
        self.assertEqual(row["fill_price"], 103.5)
        self.assertEqual(row["daily"]["close"], 103)

    def test_close_does_not_substitute_last_minute_for_daily_close(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(symbol, *, timeframe, start=None, end=None, feed="iex", limit=1000, max_pages=1):
            if timeframe == "1Day":
                return {"data": []}
            return {"data": [{"timestamp": "2026-08-03T19:59:00Z", "open": 103, "high": 104, "low": 102, "close": 103.5, "volume": 4}]}

        with patch("services.realtime_market_data.fetch_stock_bars", side_effect=fake_bars):
            with self.assertRaisesRegex(RuntimeError, "缺少 2026-08-03 CLOSE"):
                hub.event_snapshot(["SPY"], trading_date="2026-08-03", event="CLOSE")

    def test_competition_snapshot_can_keep_valid_symbols_and_report_unsupported_iex_symbol(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(symbol, *, timeframe, start=None, end=None, feed="iex", limit=1000, max_pages=1):
            if timeframe == "1Day":
                return {"data": [
                    {"timestamp": "2026-07-31T00:00:00Z", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
                    {"timestamp": "2026-08-03T00:00:00Z", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
                ]}
            return {"data": [{"timestamp": "2026-08-03T13:30:00Z", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}]}

        with patch("services.realtime_market_data.fetch_stock_bars", side_effect=fake_bars):
            snapshot = hub.event_snapshot(
                ["SPY", "BTC/USD"], trading_date="2026-08-03", event="OPEN", allow_missing=True
            )
        self.assertEqual(set(snapshot["symbols"]), {"SPY"})
        self.assertTrue(any("BTC/USD" in item for item in snapshot["missing"]))

    def test_code_notification_contains_strategy_intro_and_data_warning(self) -> None:
        result = {
            "decision": {
                "trading_date": "2026-08-03", "event": "14:00", "recommendations": [],
            },
            "data_manifest": {"missing": ["BTC/USD: IEX 股票行情不支持该加密货币代码"]},
            "calculation": {"engine_logs": []},
        }
        task = {
            "name": "通知文案测试",
            "strategy_snapshot": {"design_mode": "code", "code_key": "rapid_drop_atr_rotation"},
            "notification_settings": {},
        }
        _subject, body = render_message(task, result)
        self.assertIn("急跌回避 + ATR 动量轮动", body)
        self.assertIn("BTC/USD", body)

    def test_visual_notification_template_exposes_decision_and_indicator_basis(self) -> None:
        task = {
            "name": "SPY 8和13EMA 测试策略",
            "strategy_snapshot": {"design_mode": "visual", "definition": {}},
            "notification_settings": {
                "body_template": (
                    "发送时间：{{event.name}}\n"
                    "决策内容：{{decision}}\n"
                    "决策依据：{{basis}}"
                )
            },
        }
        result = {
            "decision": {
                "trading_date": "2026-08-03",
                "event": "OPEN",
                "recommendations": [{
                    "action": "BUY",
                    "symbol": "SPY",
                    "target_weight_percent": 100,
                    "effective_leverage": 1,
                    "reason": "规则命中：EMA 金叉",
                }],
            },
            "calculation": {"engine_logs": [{
                "event_type": "RULE_EVALUATION",
                "symbol": "SPY",
                "context": {
                    "rule_id": "buy-spy",
                    "rule_name": "EMA 金叉",
                    "condition": "ema(8) > ema(13)",
                    "matched": True,
                    "inputs": {"ema(8)": 635.125, "ema(13)": 632.5},
                },
            }]},
        }

        _subject, body = render_message(task, result)

        self.assertIn("发送时间：OPEN", body)
        self.assertIn("决策内容：BUY SPY 100%", body)
        self.assertIn("ema(8)=635.125", body)
        self.assertIn("ema(13)=632.5", body)

    def test_rapid_strategy_notification_matches_each_event_role(self) -> None:
        task = {
            "name": "事件文案测试",
            "strategy_snapshot": {
                "design_mode": "code", "code_key": "rapid_drop_atr_rotation",
                "definition": {"params": {"risk_check_time": "12:26", "selection_time": "12:28"}},
            },
            "notification_settings": {},
        }
        risk_result = {
            "decision": {"trading_date": "2026-08-03", "event": "12:26", "recommendations": []},
            "data_manifest": {},
            "calculation": {"engine_logs": [{
                "event_type": "RAPID_DROP_ATR_RISK_CHECK", "message": "SPY 风险检查通过。", "symbol": "SPY",
            }]},
        }
        _subject, risk_body = render_message(task, risk_result)
        self.assertIn("风险检查结果", risk_body)
        self.assertIn("SPY 风险检查通过", risk_body)
        self.assertNotIn("ATR 动量评分与排名", risk_body)

        selection_result = {
            "decision": {
                "trading_date": "2026-08-03", "event": "12:28",
                "recommendations": [{
                    "action": "BUY", "symbol": "XLE", "target_weight_percent": 100,
                    "effective_leverage": 1, "reason": "ATR 动量第一名",
                }],
            },
            "data_manifest": {},
            "calculation": {"engine_logs": [{
                "event_type": "RAPID_DROP_ATR_DAILY_SCORE", "message": "XLE ATR 动量评分 2.0，合格排名第 1。",
                "symbol": "XLE", "context": {"rank": 1},
            }]},
        }
        _subject, selection_body = render_message(task, selection_result)
        self.assertIn("ATR 动量评分与排名", selection_body)
        self.assertIn("BUY XLE", selection_body)
        self.assertNotIn("风险检查结果", selection_body)

    def test_event_calculation_aliases_are_persisted_for_audit(self) -> None:
        strategy = create_default_strategy(name="事件审计策略", design_mode="visual", selection_mode="single")
        task = realtime_repository.create_task(
            name="事件审计任务", strategy=strategy, follow_strategy=False,
            settings={}, notification_settings={}, portfolio_state={}
        )
        run = realtime_repository.create_run(task)
        event = realtime_repository.create_event(
            run_id=run["id"], task_id=task["id"], dedupe_key="audit-event",
            trading_date="2026-08-03", event_name="OPEN", scheduled_at="2026-08-03T13:30:00Z"
        )
        stored = realtime_repository.update_event(
            event["id"], status="completed", data_manifest={"feed": "iex"},
            decision={"recommendations": []}, calculation={"engine_logs": [{"message": "ok"}]}
        )
        self.assertEqual(stored["data_manifest"]["feed"], "iex")
        self.assertEqual(stored["decision"]["recommendations"], [])
        self.assertEqual(stored["calculation"]["engine_logs"][0]["message"], "ok")

    def test_runtime_recovery_recognizes_stale_heartbeat(self) -> None:
        self.assertTrue(RealtimeTaskManager._is_stale_runtime({"heartbeat_at": "2020-01-01T00:00:00+00:00"}))
        self.assertTrue(RealtimeTaskManager._is_stale_runtime({"heartbeat_at": None, "run_started_at": None}))


if __name__ == "__main__":
    unittest.main()
