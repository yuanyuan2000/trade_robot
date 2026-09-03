from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import database.db as main_db
from database import realtime_repository
import services.realtime_scheduler as scheduler_module
from services.backtest.code_strategies import (
    STRATEGY_REGISTRY,
    RapidDropAtrRotationStrategy,
)
from services.backtest.errors import BacktestValidationError
from services.backtest.portfolio import Portfolio
from services.backtest.service import create_default_strategy, update_strategy
from services.realtime_decision_service import (
    RealtimeDecisionEvaluator,
    _restore_portfolio,
    _restore_strategy_state,
    _strategy_state,
)
from services.realtime_mail import (
    _plain_text_email_html,
    render_message,
    send_smtp,
    validate_message_template,
)
from services.realtime_scheduler import (
    RealtimeTaskManager,
    _events_for_strategy,
    _rebase_followed_settings,
)
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

    def test_legacy_dynamic_leverage_state_does_not_infer_sub_one_strategy_layer(self) -> None:
        portfolio = Portfolio(
            100_000,
            symbol_leverage_multipliers={"GLD": 3, "SPY": 3},
        )
        legacy_state = {
            "cash": 100_000,
            "positions": {},
            "symbol_leverage_multipliers": {"GLD": 2, "SPY": 3},
        }

        _restore_portfolio(portfolio, legacy_state)

        self.assertEqual(float(portfolio.configured_symbol_leverage_multipliers["GLD"]), 2)
        self.assertEqual(float(portfolio.symbol_leverage_multipliers["GLD"]), 2)
        portfolio.set_configured_symbol_leverage_multiplier("GLD", 1)
        self.assertEqual(float(portfolio.symbol_leverage_multipliers["GLD"]), 1)

    def test_current_dynamic_leverage_state_preserves_strategy_layer(self) -> None:
        portfolio = Portfolio(
            100_000,
            symbol_leverage_multipliers={"GLD": 3},
        )
        current_state = {
            "cash": 100_000,
            "positions": {},
            "configured_symbol_leverage_multipliers": {"GLD": 2},
            "symbol_leverage_multipliers": {"GLD": 6},
        }

        _restore_portfolio(portfolio, current_state)
        portfolio.set_configured_symbol_leverage_multiplier("GLD", 1)

        self.assertEqual(float(portfolio.configured_symbol_leverage_multipliers["GLD"]), 1)
        self.assertEqual(float(portfolio.symbol_leverage_multipliers["GLD"]), 3)

    def test_runtime_state_never_overwrites_current_code_strategy_parameters(self) -> None:
        for key, strategy_type in STRATEGY_REGISTRY.items():
            with self.subTest(code_key=key):
                instance = strategy_type({})
                current_params = dict(instance.params)
                state = _strategy_state(instance)
                self.assertNotIn("params", state)
                _restore_strategy_state(
                    instance,
                    {**state, "params": {"obsolete": True}, "removed_field": 123},
                )
                self.assertEqual(instance.params, current_params)
                self.assertFalse(hasattr(instance, "removed_field"))

    def test_followed_settings_take_new_defaults_and_keep_explicit_overrides(self) -> None:
        rebased = _rebase_followed_settings(
            {
                "initial_capital": 100_000,
                "leverage_multiplier": 2,
                "dynamic_leverage": {"volatility_period": 30, "stress_days": 13},
            },
            {
                "initial_capital": 100_000,
                "leverage_multiplier": 1,
                "dynamic_leverage": {"volatility_period": 30, "stress_days": 10},
            },
            {
                "initial_capital": 200_000,
                "leverage_multiplier": 1,
                "dynamic_leverage": {"volatility_period": 60, "stress_days": 10},
            },
        )

        self.assertEqual(rebased["initial_capital"], 200_000)
        self.assertEqual(rebased["leverage_multiplier"], 2)
        self.assertEqual(rebased["dynamic_leverage"]["volatility_period"], 60)
        self.assertEqual(rebased["dynamic_leverage"]["stress_days"], 13)

    def test_close_strategy_schedules_next_open_fill_boundary(self) -> None:
        strategy = create_default_strategy(
            name="收盘信号策略", design_mode="visual", selection_mode="single"
        )
        strategy["definition"]["rules"][0]["when"] = "CLOSE"
        self.assertEqual(_events_for_strategy(strategy), ["OPEN", "CLOSE"])

    def test_realtime_close_signal_is_filled_at_next_open(self) -> None:
        strategy = create_default_strategy(
            name="实时收盘成交一致性", design_mode="visual", selection_mode="single"
        )
        strategy["definition"]["rules"] = [{
            "id": "close-buy", "name": "收盘买入", "enabled": True,
            "priority": 1, "action": "BUY", "sizing_mode": "TARGET",
            "value": 100, "condition": "true", "when": "CLOSE",
        }]
        task = realtime_repository.create_task(
            name="实时收盘成交一致性任务", strategy=strategy,
            follow_strategy=False, settings=strategy["default_settings"],
            notification_settings={}, portfolio_state={},
        )
        run = realtime_repository.create_run(task)
        history = [{
            "date": "2026-08-10", "open": 99, "high": 101, "low": 98,
            "close": 100, "volume": 1000, "is_complete": 1,
        }]
        session = {
            "trading_date": "2026-08-11", "open_minute_utc": 1,
            "close_minute_utc": 391, "is_early_close": False,
        }
        hub = Mock()
        hub.event_snapshot.side_effect = [
            {
                "symbols": {"SPY": {
                    "signal_price": 105, "fill_price": None,
                    "signal_time": "2026-08-11 CLOSE", "fill_time": None,
                    "daily": {**history[0], "date": "2026-08-11", "close": 105},
                    "daily_is_complete": True,
                }},
                "source": "test", "feed": "test", "missing": [],
                "requested_at": "2026-08-11T20:00:00Z",
            },
            {
                "symbols": {"SPY": {
                    "signal_price": 105, "fill_price": 110,
                    "signal_time": "2026-08-12 OPEN", "fill_time": "2026-08-12 09:30",
                    "daily": {**history[0], "date": "2026-08-12", "open": 110},
                    "daily_is_complete": False,
                }},
                "source": "test", "feed": "test", "missing": [],
                "requested_at": "2026-08-12T13:30:00Z",
            },
        ]
        evaluator = RealtimeDecisionEvaluator(hub)

        def history_snapshot(_strategy, *, trading_date, **_kwargs):
            return {
                "required_sessions": 2, "expected_dates": [history[0]["date"]],
                "symbols": {"SPY": {"snapshot_id": f"SPY:{trading_date}"}},
                "daily": {"SPY": history}, "snapshot_id": f"history:{trading_date}",
                "market": strategy["market"],
            }

        with (
            patch("services.realtime_decision_service.prepare_strategy_history", side_effect=history_snapshot),
            patch("services.realtime_decision_service.market_sessions", return_value=[session]),
        ):
            close_result = evaluator.evaluate(
                task, run, trading_date="2026-08-11", event="CLOSE"
            )
            self.assertEqual(close_result["decision"]["trades"], [])
            self.assertEqual(
                close_result["state"]["pending_close_orders"][0]["symbol"], "SPY"
            )
            continued_run = {**run, "state": close_result["state"]}
            open_result = evaluator.evaluate(
                task, continued_run, trading_date="2026-08-12", event="OPEN"
            )

        self.assertEqual(open_result["state"]["pending_close_orders"], [])
        self.assertEqual(len(open_result["decision"]["trades"]), 1)
        self.assertEqual(open_result["decision"]["trades"][0]["reference_price"], 110)

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

    def test_non_alpaca_cash_series_use_yahoo_current_price(self) -> None:
        for symbol, yahoo_symbol, current_price in (
            ("USDINDEX", "DX-Y.NYB", 97.65),
            ("US10Y", "^TNX", 4.321),
        ):
            with self.subTest(symbol=symbol):
                hub = IEXMarketDataHub()
                with (
                    patch(
                        "services.realtime_market_data.repository.resolve_symbol_alias",
                        return_value={"yahoo_symbol": yahoo_symbol},
                    ),
                    patch(
                        "services.realtime_market_data.fetch_latest_chart_prices_batch",
                        return_value={
                            yahoo_symbol: {
                                "price": current_price,
                                "market_time": 1788271200,
                            }
                        },
                    ),
                    patch(
                        "services.realtime_market_data.fetch_stock_bars",
                        side_effect=AssertionError(f"{symbol} must not be sent to Alpaca"),
                    ),
                ):
                    snapshot = hub.event_snapshot(
                        [symbol],
                        trading_date="2026-09-01",
                        event="10:00",
                    )

                row = snapshot["symbols"][symbol]
                self.assertEqual(row["signal_price"], current_price)
                self.assertEqual(row["fill_price"], current_price)
                self.assertEqual(row["source"], "yahoo_current_price")
                self.assertEqual(
                    row["price_fallback"],
                    "current_price_without_alpaca_minutes",
                )

    def test_close_does_not_substitute_last_minute_for_daily_close(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(symbol, *, timeframe, start=None, end=None, feed="iex", limit=1000, max_pages=1):
            if timeframe == "1Day":
                return {"data": []}
            return {"data": [{"timestamp": "2026-08-03T19:59:00Z", "open": 103, "high": 104, "low": 102, "close": 103.5, "volume": 4}]}

        with patch("services.realtime_market_data.fetch_stock_bars", side_effect=fake_bars):
            with self.assertRaisesRegex(RuntimeError, "缺少 2026-08-03 CLOSE"):
                hub.event_snapshot(["SPY"], trading_date="2026-08-03", event="CLOSE")

    def test_timed_event_rejects_materially_stale_signal_minute(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(
            symbol,
            *,
            timeframe,
            start=None,
            end=None,
            feed="iex",
            limit=1000,
            max_pages=1,
        ):
            if timeframe == "1Day":
                return {"data": [{
                    "timestamp": "2026-08-03T00:00:00Z",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 10,
                }]}
            return {"data": [{
                "timestamp": "2026-08-03T13:50:00Z",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 10,
            }]}

        with patch(
            "services.realtime_market_data.fetch_stock_bars",
            side_effect=fake_bars,
        ):
            with self.assertRaisesRegex(RuntimeError, "已滞后 9 分钟"):
                hub.event_snapshot(
                    ["SPY"],
                    trading_date="2026-08-03",
                    event="10:00",
                    now=datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc),
                )

    def test_competition_snapshot_supports_stock_and_crypto_candidates(self) -> None:
        hub = IEXMarketDataHub()

        def fake_bars(symbol, *, timeframe, start=None, end=None, feed="iex", limit=1000, max_pages=1):
            if timeframe == "1Day":
                return {"data": [
                    {"timestamp": "2026-07-31T00:00:00Z", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
                    {"timestamp": "2026-08-03T00:00:00Z", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
                ]}
            return {"data": [{"timestamp": "2026-08-03T13:30:00Z", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}]}

        crypto_rows = [
            {"timestamp": "2026-08-03T13:29:00Z", "open": 120000, "high": 120010, "low": 119990, "close": 120005, "volume": 2},
            {"timestamp": "2026-08-03T13:30:00Z", "open": 120006, "high": 120020, "low": 120000, "close": 120015, "volume": 3},
        ]
        with (
            patch("services.realtime_market_data.fetch_stock_bars", side_effect=fake_bars),
            patch(
                "services.realtime_market_data.fetch_crypto_bars_page",
                return_value={"data": crypto_rows},
            ),
        ):
            snapshot = hub.event_snapshot(
                ["SPY", "BTC/USD"],
                trading_date="2026-08-03",
                event="OPEN",
                allow_missing=True,
                previous_session_closes={
                    "BTC/USD": {"date": "2026-07-31", "close": 119500},
                },
            )
        self.assertEqual(set(snapshot["symbols"]), {"SPY", "BTC/USD"})
        self.assertEqual(snapshot["symbols"]["BTC/USD"]["signal_price"], 119500)
        self.assertEqual(snapshot["symbols"]["BTC/USD"]["fill_price"], 120006)
        self.assertEqual(snapshot["symbols"]["BTC/USD"]["source"], "alpaca_crypto")
        self.assertEqual(snapshot["feed"], "mixed")
        self.assertEqual(snapshot["missing"], [])

    def test_crypto_close_uses_actual_early_close_minute(self) -> None:
        hub = IEXMarketDataHub()
        close_minute_utc = int(
            datetime(2024, 7, 3, 17, 0, tzinfo=timezone.utc).timestamp()
        ) // 60
        with patch(
            "services.realtime_market_data.fetch_crypto_bars_page",
            return_value={"data": [{
                "timestamp": "2024-07-03T16:59:00Z",
                "open": 60000,
                "high": 60020,
                "low": 59980,
                "close": 60010,
                "volume": 4,
            }]},
        ):
            snapshot = hub.event_snapshot(
                ["BTC/USD"],
                trading_date="2024-07-03",
                event="CLOSE",
                market_session={
                    "trading_date": "2024-07-03",
                    "open_minute_utc": close_minute_utc - 210,
                    "close_minute_utc": close_minute_utc,
                    "is_early_close": True,
                },
                now=datetime(2024, 7, 3, 17, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(snapshot["symbols"]["BTC/USD"]["signal_price"], 60010)
        self.assertEqual(snapshot["feed"], "us")

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

    def test_data_audit_logs_do_not_expand_email_body(self) -> None:
        result = {
            "decision": {
                "trading_date": "2026-08-03",
                "event": "OPEN",
                "recommendations": [],
            },
            "data_manifest": {"missing": []},
            "calculation": {"engine_logs": [{
                "event_type": "DATA_HISTORY_AUDIT",
                "message": "SLV 历史数据快照 audit-secret-id",
                "symbol": "SLV",
                "context": {"snapshot_id": "audit-secret-id"},
            }]},
        }
        task = {
            "name": "审计日志邮件隔离",
            "strategy_snapshot": {"design_mode": "visual", "definition": {}},
            "notification_settings": {},
        }

        _subject, body = render_message(task, result)

        self.assertNotIn("audit-secret-id", body)
        self.assertNotIn("DATA_HISTORY_AUDIT", body)

    def test_visual_notification_template_exposes_decision_and_indicator_basis(self) -> None:
        task = {
            "name": "SPY 8和13EMA 测试策略",
            "strategy_snapshot": {"design_mode": "visual", "definition": {}},
            "notification_settings": {
                "subject_template": "SPY 指标 1.234567",
                "body_template": (
                    "决策日期：{{decision.date}}\n"
                    "决策时点：{{decision.time_label}}\n"
                    "决策内容：{{decision.actions}}\n"
                    "决策依据：{{decision.basis}}\n"
                    "固定阈值：1.234567"
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
                    "target_weight_percent": 99.12389,
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
                    "inputs": {"ema(8)": 635.12567, "ema(13)": 632.5},
                },
            }]},
        }

        subject, body = render_message(task, result)

        self.assertEqual(subject, "SPY 指标 1.235")
        self.assertIn("决策日期：2026-08-03", body)
        self.assertIn("决策时点：OPEN（美东常规开盘 09:30）", body)
        self.assertIn("决策内容：BUY SPY 99.124%", body)
        self.assertIn("ema(8)=635.126", body)
        self.assertIn("ema(13)=632.5", body)
        self.assertIn("固定阈值：1.235", body)

    def test_legacy_visual_template_aliases_remain_compatible(self) -> None:
        task = {
            "name": "旧模板",
            "strategy_snapshot": {"design_mode": "visual"},
            "notification_settings": {
                "body_template": "{{event.name}}|{{decision}}|{{basis}}",
            },
        }
        result = {
            "decision": {
                "trading_date": "2026-08-03",
                "event": "OPEN",
                "recommendations": [],
            },
            "calculation": {"engine_logs": []},
        }

        _subject, body = render_message(task, result)

        self.assertEqual(body, "OPEN|无新调仓决策|没有非代码规则命中")

    def test_message_template_rejects_unknown_or_unclosed_placeholders(self) -> None:
        validate_message_template(
            "{{task.name}} {{decision.time}} {{decision.actions}}"
        )
        with self.assertRaisesRegex(ValueError, "不支持的占位符"):
            validate_message_template("{{decision.action}}")
        with self.assertRaisesRegex(ValueError, "未闭合"):
            validate_message_template("{{decision.actions}")

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

    def test_wtme_notification_uses_percent_filter_without_atr_wording(self) -> None:
        task = {
            "name": "WTME 事件文案测试",
            "strategy_snapshot": {
                "design_mode": "code",
                "code_key": "rapid_drop_wtme_rotation",
                "definition": {
                    "params": {
                        "risk_check_time": "09:40",
                        "selection_time": "10:00",
                    }
                },
            },
            "notification_settings": {},
        }
        result = {
            "decision": {
                "trading_date": "2026-08-03",
                "event": "09:40",
                "recommendations": [],
            },
            "data_manifest": {},
            "calculation": {"engine_logs": [{
                "event_type": "RAPID_DROP_WTME_RISK_CHECK",
                "message": "SPY 风险检查通过；最差单日涨跌 -1.00%。",
                "symbol": "SPY",
            }]},
        }

        _subject, body = render_message(task, result)

        self.assertIn("按百分比单日急跌规则", body)
        self.assertIn("SPY 风险检查通过", body)
        self.assertNotIn("百分比/ATR", body)

        selection_result = {
            "decision": {
                "trading_date": "2026-08-03",
                "event": "10:00",
                "recommendations": [{
                    "action": "BUY",
                    "symbol": "GLD",
                    "target_weight_percent": 100,
                    "effective_leverage": 2,
                    "holding_percent": 200,
                    "reason": "WTME 买入条件通过",
                }],
            },
            "data_manifest": {},
            "calculation": {"engine_logs": [
                {
                    "event_type": "RAPID_DROP_WTME_DAILY_SCORE",
                    "message": "GLD WTME 评分完整计算说明",
                    "symbol": "GLD",
                    "context": {
                        "score": 12.34567,
                        "rank": 1,
                        "holding_percent": 200,
                        "score_formula": "100 × Rw ÷ Aw",
                    },
                },
                {
                    "event_type": "RAPID_DROP_WTME_DAILY_SCORE",
                    "message": "SPY WTME 评分完整计算说明",
                    "symbol": "SPY",
                    "context": {
                        "score": 8.2,
                        "rank": 2,
                        "holding_percent": 0,
                        "score_formula": "100 × Rw ÷ Aw",
                    },
                },
            ]},
        }

        _subject, selection_body = render_message(task, selection_result)

        self.assertIn("| 标的 | 持仓比例 | WTME评分 | 急跌过滤后排名 |", selection_body)
        self.assertIn("| GLD | 200% | 12.346 | 1 |", selection_body)
        self.assertIn("| SPY | 0% | 8.2 | 2 |", selection_body)
        self.assertNotIn("100 × Rw", selection_body)
        self.assertNotIn("轮动与调仓建议", selection_body)
        self.assertNotIn("BUY GLD", selection_body)

        html = _plain_text_email_html(selection_body.replace("GLD", "BTC/USD"))
        self.assertIn("<table", html)
        self.assertIn("table-layout:fixed", html)
        self.assertIn("overflow-wrap:anywhere", html)
        self.assertIn("BTC/USD", html)

    def test_smtp_sends_plain_text_and_html_alternatives(self) -> None:
        smtp = Mock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp
        channel = {
            "sender_email": "sender@example.com",
            "security_mode": "ssl",
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "username": "sender@example.com",
        }
        with (
            patch("services.realtime_mail._channel_secret", return_value=(channel, "secret")),
            patch("services.realtime_mail.smtplib.SMTP_SSL", return_value=smtp_context),
        ):
            send_smtp(
                1,
                recipient="target@example.com",
                subject="动态持仓",
                body="| 标的 | 持仓比例 |\n| --- | ---: |\n| BTC/USD | 130% |",
            )

        message = smtp.send_message.call_args.args[0]
        self.assertTrue(message.is_multipart())
        self.assertEqual(
            [part.get_content_type() for part in message.iter_parts()],
            ["text/plain", "text/html"],
        )
        self.assertIn(
            "table-layout:fixed",
            message.get_body(preferencelist=("html",)).get_content(),
        )

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

    def test_stop_is_immediate_and_clears_the_next_event(self) -> None:
        strategy = create_default_strategy(
            name="立即终止策略", design_mode="visual", selection_mode="single"
        )
        task = realtime_repository.create_task(
            name="立即终止任务", strategy=strategy, follow_strategy=False,
            settings={}, notification_settings={}, portfolio_state={},
        )
        run = realtime_repository.create_run(task)
        manager = RealtimeTaskManager(max_workers=1)
        manager._states[task["id"]] = {
            "run_id": run["id"], "strategy": strategy, "events": ["OPEN"],
            "processed": set(), "event_in_flight": False,
            "event_started": False, "event_key": None, "future": None,
            "stop_requested": False,
        }
        realtime_repository.set_task_runtime(
            task["id"], desired_state="running", runtime_state="running",
            next_event_at="2026-08-14T13:30:00Z",
        )
        realtime_repository.update_run(run["id"], status="running")
        try:
            stopped = manager.stop(task["id"])
            self.assertEqual(stopped["desired_state"], "stopped")
            self.assertEqual(stopped["runtime_state"], "stopped")
            self.assertIsNone(stopped["next_event_at"])
            self.assertNotIn(task["id"], manager._states)
            self.assertEqual(
                realtime_repository.get_run(run["id"])["status"], "stopped"
            )
        finally:
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_stop_during_event_waits_for_event_to_finish(self) -> None:
        strategy = create_default_strategy(
            name="计算中终止策略", design_mode="visual", selection_mode="single"
        )
        task = realtime_repository.create_task(
            name="计算中终止任务", strategy=strategy, follow_strategy=False,
            settings={}, notification_settings={"enabled": True},
            portfolio_state={"cash": 100000, "positions": {}},
        )
        run = realtime_repository.create_run(task)
        manager = RealtimeTaskManager(max_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def finish_after_release(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return {
                "data_manifest": {},
                "decision": {
                    "trading_date": "2026-08-14", "event": "OPEN",
                    "recommendations": [],
                },
                "calculation": {"engine_logs": []},
                "state": {"portfolio": {"cash": 1, "positions": {}}},
            }

        manager.evaluator.evaluate = Mock(side_effect=finish_after_release)
        manager.mail.enqueue_for_event = Mock()
        state = {
            "run_id": run["id"], "strategy": strategy, "events": ["OPEN"],
            "processed": {"event"}, "event_in_flight": True,
            "event_started": False, "event_key": "event", "future": None,
            "stop_requested": False,
        }
        manager._states[task["id"]] = state
        realtime_repository.set_task_runtime(
            task["id"], desired_state="running", runtime_state="running"
        )
        realtime_repository.update_run(run["id"], status="running")
        event_target = datetime.now(timezone.utc)
        try:
            future = manager._executor.submit(
                manager._execute_event, task["id"], state, task,
                {
                    "trading_date": "2026-08-14",
                    "target": event_target,
                },
                "OPEN",
            )
            self.assertTrue(entered.wait(timeout=2))
            stopping = manager.stop(task["id"])
            self.assertEqual(stopping["desired_state"], "stopped")
            self.assertEqual(stopping["runtime_state"], "stopping")
            self.assertIn(task["id"], manager._states)
            release.set()
            future.result(timeout=2)
            event = realtime_repository.list_events(task["id"], limit=1)[0]
            self.assertEqual(event["status"], "completed")
            self.assertEqual(
                realtime_repository.get_task(task["id"])["portfolio_state"]["cash"],
                1,
            )
            finished = realtime_repository.get_task(task["id"])
            self.assertEqual(finished["runtime_state"], "stopped")
            self.assertNotIn(task["id"], manager._states)
            manager.mail.enqueue_for_event.assert_called_once()
        finally:
            release.set()
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_idle_tasks_are_managed_without_event_worker_submission(self) -> None:
        strategy = create_default_strategy(
            name="中央调度策略", design_mode="visual", selection_mode="single"
        )
        task = realtime_repository.create_task(
            name="中央调度任务", strategy=strategy, follow_strategy=False,
            settings={}, notification_settings={}, portfolio_state={},
        )
        run = realtime_repository.create_run(task)
        manager = RealtimeTaskManager(max_workers=1)
        state = {
            "run_id": run["id"], "strategy": strategy, "events": ["OPEN"],
            "processed": set(), "event_in_flight": False,
            "event_started": False, "event_key": None, "future": None,
            "stop_requested": False,
        }
        manager._states[task["id"]] = state
        realtime_repository.set_task_runtime(
            task["id"], desired_state="running", runtime_state="running"
        )
        future_time = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)
        try:
            with patch.object(
                manager, "_next_event",
                return_value=({"trading_date": "2026-08-14", "target": future_time}, "OPEN"),
            ), patch.object(manager._executor, "submit") as submit:
                wait = manager._schedule_once(
                    now=datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)
                )
            submit.assert_not_called()
            self.assertEqual(wait, 30.0)
            self.assertFalse(state["event_in_flight"])
        finally:
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_start_registers_task_without_occupying_event_pool(self) -> None:
        strategy = create_default_strategy(
            name="启动不占线程策略", design_mode="visual", selection_mode="single"
        )
        tasks = [
            realtime_repository.create_task(
                name=f"启动不占线程任务 {index}", strategy=strategy,
                follow_strategy=False, settings={}, notification_settings={},
                portfolio_state={},
            )
            for index in range(5)
        ]
        manager = RealtimeTaskManager(max_workers=1)
        try:
            with patch.object(
                scheduler_module, "_validate_local_history", return_value=None
            ), patch.object(manager._executor, "submit") as submit:
                started = [manager.start(task["id"]) for task in tasks]
            self.assertTrue(all(task["runtime_state"] == "running" for task in started))
            self.assertEqual(set(manager._states), {task["id"] for task in tasks})
            submit.assert_not_called()
        finally:
            for task in tasks:
                manager.stop(task["id"])
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_running_followed_task_rolls_to_new_strategy_and_rebases_defaults(self) -> None:
        strategy = create_default_strategy(
            name="运行中跟随更新策略", design_mode="visual", selection_mode="single"
        )
        task_settings = {
            **strategy["default_settings"],
            "leverage_multiplier": 2,
        }
        task = realtime_repository.create_task(
            name="运行中跟随更新任务", strategy=strategy, follow_strategy=True,
            settings=task_settings, notification_settings={}, portfolio_state={"cash": 100_000},
        )
        old_run = realtime_repository.create_run(task)
        realtime_repository.update_run(
            old_run["id"], status="running",
            state={
                "portfolio": {"cash": 99_000, "positions": {}},
                "strategy_state": {"competition_eligible_by_date": {}},
            },
        )
        new_defaults = deepcopy(strategy["default_settings"])
        new_defaults["initial_capital"] = 200_000
        updated_strategy = update_strategy(
            strategy["id"],
            {"revision": strategy["revision"], "default_settings": new_defaults},
        )
        manager = RealtimeTaskManager(max_workers=1)
        state = {
            "run_id": old_run["id"], "strategy": strategy,
            "events": _events_for_strategy(strategy), "processed": set(),
            "event_in_flight": False, "event_started": False,
            "event_key": None, "future": None, "stop_requested": False,
        }
        try:
            refreshed = manager._refresh_followed_run(task, state)
            new_run = realtime_repository.get_run(state["run_id"])

            self.assertNotEqual(new_run["id"], old_run["id"])
            self.assertEqual(new_run["strategy_snapshot"]["revision"], updated_strategy["revision"])
            self.assertEqual(new_run["settings"]["initial_capital"], 200_000)
            self.assertEqual(new_run["settings"]["leverage_multiplier"], 2)
            self.assertEqual(new_run["state"]["portfolio"]["cash"], 99_000)
            self.assertEqual(realtime_repository.get_run(old_run["id"])["status"], "stopped")
            self.assertEqual(refreshed["source_strategy_revision"], updated_strategy["revision"])
        finally:
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_followed_code_strategy_uses_current_version_and_new_parameter_defaults(self) -> None:
        strategy = create_default_strategy(
            name="代码实现升级跟随",
            design_mode="code",
            selection_mode="competition",
            code_key="rapid_drop_atr_rotation",
        )
        task = realtime_repository.create_task(
            name="代码实现升级跟随任务", strategy=strategy, follow_strategy=True,
            settings=strategy["default_settings"], notification_settings={},
            portfolio_state={},
        )
        upgraded_schema = {
            **RapidDropAtrRotationStrategy.parameter_schema,
            "future_logic_enabled": {
                "label": "未来逻辑开关", "type": "boolean", "default": True,
            },
        }
        manager = RealtimeTaskManager(max_workers=1)
        try:
            with (
                patch.object(RapidDropAtrRotationStrategy, "version", "99.0.0"),
                patch.object(RapidDropAtrRotationStrategy, "parameter_schema", upgraded_schema),
            ):
                synced = manager._sync_followed_strategy(task)

            self.assertEqual(synced["strategy_snapshot"]["code_version"], "99.0.0")
            self.assertTrue(
                synced["strategy_snapshot"]["definition"]["params"]["future_logic_enabled"]
            )
            self.assertEqual(synced["source_strategy_revision"], strategy["revision"])
        finally:
            manager._executor.shutdown(wait=False, cancel_futures=True)

    def test_start_rejects_stale_code_version_before_history_or_run_creation(self) -> None:
        strategy = create_default_strategy(
            name="旧代码版本策略",
            design_mode="code",
            selection_mode="competition",
            code_key="rapid_drop_wtme_rotation",
        )
        stale_snapshot = {**strategy, "code_version": "0.9.0"}
        task = realtime_repository.create_task(
            name="旧代码版本任务",
            strategy=stale_snapshot,
            follow_strategy=False,
            settings={},
            notification_settings={},
            portfolio_state={},
        )
        manager = RealtimeTaskManager(max_workers=1)
        try:
            with patch.object(
                scheduler_module, "_validate_local_history"
            ) as validate_history:
                with self.assertRaisesRegex(
                    BacktestValidationError,
                    "不能按原版本运行.*重新保存该策略参数",
                ):
                    manager.start(task["id"])
            validate_history.assert_not_called()
            self.assertEqual(realtime_repository.list_runs(task["id"]), [])
            self.assertNotIn(task["id"], manager._states)
        finally:
            manager._executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
