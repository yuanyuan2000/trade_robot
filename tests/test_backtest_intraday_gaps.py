from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
import database.intraday_db as intraday_db
from database import intraday_repository
from services.backtest.data import (
    HistoricalDataSet,
    _epoch_minute,
    _minute_failure_segments,
    _minute_failure_summary,
    load_historical_dataset,
)
from services.backtest.engine import BacktestEngine
from services.backtest.validation import DEFAULT_BACKTEST_SETTINGS


class BacktestIntradayGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "intraday.sqlite"
        self.patchers = [
            patch.object(intraday_db, "INTRADAY_DATABASE_PATH", self.database_path),
            patch.object(main_db, "INTRADAY_DATABASE_PATH", self.database_path),
        ]
        for patcher in self.patchers:
            patcher.start()
        intraday_db.init_intraday_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_non_alpaca_cash_series_use_previous_close_without_minutes(self) -> None:
        trading_date = "2024-01-03"
        sessions = [{
            "trading_date": trading_date,
            "open_minute_utc": _epoch_minute(trading_date, "09:30"),
            "close_minute_utc": _epoch_minute(trading_date, "16:00"),
            "is_early_close": False,
        }]
        daily = [
            {
                "date": "2024-01-02", "open": 3.9, "high": 4.1,
                "low": 3.8, "close": 4.0, "volume": 0, "is_complete": 1,
            },
            {
                "date": trading_date, "open": 4.2, "high": 4.3,
                "low": 4.1, "close": 4.25, "volume": 0, "is_complete": 1,
            },
        ]
        for symbol, asset_class in (("USDINDEX", "index"), ("US10Y", "fixed_income")):
            with self.subTest(symbol=symbol):
                with (
                    patch("services.backtest.data.repository.get_daily_prices", return_value=daily),
                    patch("services.backtest.data.repository.get_symbol", return_value={"asset_class": asset_class}),
                    patch("services.backtest.data.ensure_market_sessions", return_value=sessions),
                    patch("services.backtest.data.ensure_corporate_actions", return_value=[]),
                    patch("services.backtest.data.intraday_repository.get_sync_state", side_effect=AssertionError(f"{symbol} must not request Alpaca sync state")),
                    patch("services.backtest.data.intraday_repository.get_minute_bars_at", side_effect=AssertionError(f"{symbol} must not request minute bars")),
                ):
                    dataset = load_historical_dataset(
                        universe=[symbol],
                        additional_symbols=[],
                        start_date=trading_date,
                        end_date=trading_date,
                        intraday_events=["10:00"],
                        minimum_lookback=1,
                    )

                price = dataset.event_price(symbol, trading_date, "10:00")
                self.assertEqual(price.signal_price, 4.0)
                self.assertEqual(price.fill_price, 4.0)
                fallback = dataset.manifest["symbols"][symbol]["intraday_price_fallback"]
                self.assertEqual(fallback["mode"], "previous_session_close")
                self.assertEqual(fallback["reason"], f"{symbol} has no Alpaca minute history")

    def test_sparse_halt_minutes_resolve_to_last_signal_and_first_fill(self) -> None:
        target = _epoch_minute("2020-03-09", "09:40")
        intraday_repository.upsert_minute_bars(
            "SPY",
            [
                {
                    "minute_utc": target - 6,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                },
                {
                    "minute_utc": target + 9,
                    "open": 90,
                    "high": 91,
                    "low": 89,
                    "close": 90,
                    "volume": 1,
                },
            ],
        )
        resolved = intraday_repository.resolve_minute_event_gaps(
            "SPY",
            [
                {
                    "target_minute": target,
                    "open_minute": _epoch_minute("2020-03-09", "09:30"),
                    "close_minute": _epoch_minute("2020-03-09", "16:00"),
                }
            ],
        )[target]
        self.assertEqual(resolved["signal_minute"], target - 6)
        self.assertEqual(resolved["fill_minute"], target + 9)

    def test_engine_uses_pre_halt_signal_and_post_halt_open(self) -> None:
        trading_date = "2024-01-02"
        target = _epoch_minute(trading_date, "09:40")
        daily = [
            {
                "date": "2023-12-29",
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
            {
                "date": trading_date,
                "open": 10,
                "high": 20,
                "low": 10,
                "close": 20,
                "volume": 1,
            },
        ]
        minute = {
            target - 6: {"open": 11, "high": 12, "low": 10, "close": 11},
            target + 9: {"open": 20, "high": 999, "low": 1, "close": 999},
        }
        dataset = HistoricalDataSet(
            daily={"SPY": daily},
            sessions=[trading_date],
            minute={"SPY": minute},
            intraday_event_minutes={
                "SPY": {
                    f"{trading_date}|09:40": {
                        "signal_minute": target - 6,
                        "fill_minute": target + 9,
                    }
                }
            },
            required_intraday_events=["09:40"],
        )
        strategy = {
            "name": "停牌撮合测试",
            "design_mode": "visual",
            "selection_mode": "single",
            "definition": {
                "symbols": [{"symbol": "SPY", "max_weight": 100}],
                "rules": [
                    {
                        "id": "buy",
                        "name": "buy",
                        "enabled": True,
                        "priority": 10,
                        "action": "BUY",
                        "sizing_mode": "TARGET",
                        "value": 100,
                        "condition": "price = 11",
                        "when": "09:40",
                    }
                ],
            },
            "default_settings": {},
        }
        result = BacktestEngine(
            strategy,
            {
                **DEFAULT_BACKTEST_SETTINGS,
                "start_date": trading_date,
                "end_date": trading_date,
                "initial_capital": 1000,
                "commission_per_share": 0,
                "minimum_commission": 0,
                "risk_free_rate": 0,
                "benchmark": "none",
            },
            dataset=dataset,
        ).run()
        self.assertEqual(result.trades[0]["reference_price"], 20)
        self.assertIn("09:49", result.trades[0]["event_time"])

    def test_missing_diagnostics_group_symbol_date_ranges(self) -> None:
        sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
        segments = _minute_failure_segments(
            [
                {
                    "trading_date": value,
                    "event": "10:00",
                    "missing": ["signal", "fill"],
                }
                for value in sessions
            ],
            sessions,
        )
        detail = [{"symbol": "MU", "type": "minute", "segments": segments}]
        message = _minute_failure_summary(detail)
        self.assertIn("MU", message)
        self.assertIn("2024-01-02 至 2024-01-04", message)
        self.assertIn("10:00", message)
        self.assertIn("事件前信号行情", message)
        self.assertIn("事件后可成交行情", message)

    def test_requested_default_settings(self) -> None:
        self.assertEqual(
            DEFAULT_BACKTEST_SETTINGS["end_date"],
            (date.today() - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(DEFAULT_BACKTEST_SETTINGS["minimum_commission"], 1.0)
        self.assertEqual(DEFAULT_BACKTEST_SETTINGS["commission_per_share"], 0.01)
        self.assertEqual(DEFAULT_BACKTEST_SETTINGS["risk_free_rate"], 0.045)

    @patch(
        "services.backtest.data.intraday_repository.resolve_minute_event_gaps"
    )
    @patch(
        "services.backtest.data.intraday_repository.get_minute_bars_at",
        return_value={},
    )
    @patch("services.backtest.data.ensure_corporate_actions", return_value=[])
    @patch("services.backtest.data.ensure_market_sessions")
    @patch("services.backtest.data.repository.get_daily_prices")
    def test_true_missing_data_names_symbol_date_event_and_side(
        self,
        get_daily,
        get_sessions,
        _get_actions,
        _get_bars,
        resolve_gaps,
    ) -> None:
        trading_date = "2024-01-02"
        target = _epoch_minute(trading_date, "10:00")
        get_sessions.return_value = [
            {
                "trading_date": trading_date,
                "open_minute_utc": _epoch_minute(trading_date, "09:30"),
                "close_minute_utc": _epoch_minute(trading_date, "16:00"),
                "is_early_close": False,
            }
        ]
        get_daily.return_value = [
            {
                "date": "2024-01-01",
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
                "is_complete": 1,
            },
            {
                "date": trading_date,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
                "is_complete": 1,
            },
        ]
        resolve_gaps.return_value = {
            target: {"signal_minute": None, "fill_minute": None}
        }

        with self.assertRaises(Exception) as raised:
            load_historical_dataset(
                universe=["MU"],
                additional_symbols=[],
                start_date=trading_date,
                end_date=trading_date,
                intraday_events=["10:00"],
                minimum_lookback=1,
            )
        message = str(raised.exception)
        self.assertIn("MU", message)
        self.assertIn(trading_date, message)
        self.assertIn("10:00", message)
        self.assertIn("事件前信号行情", message)
        self.assertIn("事件后可成交行情", message)

    def test_verified_minute_start_delays_symbol_and_logs_join_date(self) -> None:
        sessions = ["2020-12-31", "2021-01-04"]
        daily_rows = [
            {
                "date": trading_date,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 1,
                "is_complete": 1,
            }
            for trading_date in ["2020-12-30", *sessions]
        ]
        market_sessions = [
            {
                "trading_date": trading_date,
                "open_minute_utc": _epoch_minute(trading_date, "09:30"),
                "close_minute_utc": _epoch_minute(trading_date, "16:00"),
                "is_early_close": False,
            }
            for trading_date in sessions
        ]
        target = _epoch_minute("2021-01-04", "10:00")
        minute_rows = {
            target - 1: {
                "minute_utc": target - 1,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
            target: {
                "minute_utc": target,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
        }
        with (
            patch(
                "services.backtest.data.repository.get_daily_price_series",
                return_value=[],
            ),
            patch(
                "services.backtest.data.repository.get_daily_prices",
                return_value=daily_rows,
            ),
            patch(
                "services.backtest.data.repository.get_symbol",
                return_value={
                    "asset_class": "crypto",
                    "quantity_step": 0.0001,
                    "daily_history_start_date": "2020-12-30",
                },
            ),
            patch(
                "services.backtest.data.ensure_market_sessions",
                return_value=market_sessions,
            ),
            patch(
                "services.backtest.data.ensure_corporate_actions",
                return_value=[],
            ),
            patch(
                "services.backtest.data.intraday_repository.get_sync_state",
                return_value={
                    "minute_history_start_date": "2021-01-01",
                    "minute_history_start_source": "alpaca_crypto",
                    "minute_history_start_verified": True,
                },
            ),
            patch(
                "services.backtest.data.intraday_repository.get_minute_bars_at",
                return_value=minute_rows,
            ),
        ):
            dataset = load_historical_dataset(
                universe=["BTC/USD"],
                additional_symbols=[],
                start_date=sessions[0],
                end_date=sessions[-1],
                intraday_events=["10:00"],
                minimum_lookback=1,
            )

        self.assertFalse(dataset.is_eligible("BTC/USD", sessions[0]))
        self.assertTrue(dataset.is_eligible("BTC/USD", sessions[1]))
        details = dataset.manifest["symbols"]["BTC/USD"]
        self.assertEqual(details["daily_history_start_date"], "2020-12-30")
        self.assertEqual(details["minute_history_start_date"], "2021-01-01")
        self.assertEqual(details["intraday_join_date"], "2021-01-04")

        strategy = {
            "name": "BTC 延迟加入测试",
            "design_mode": "visual",
            "selection_mode": "single",
            "definition": {
                "symbols": [{"symbol": "BTC/USD", "max_weight": 100}],
                "rules": [{
                    "id": "buy",
                    "name": "buy",
                    "enabled": True,
                    "priority": 1,
                    "action": "BUY",
                    "sizing_mode": "TARGET",
                    "value": 100,
                    "condition": "price > 0",
                    "when": "10:00",
                }],
            },
            "default_settings": {},
        }
        result = BacktestEngine(
            strategy,
            {
                **DEFAULT_BACKTEST_SETTINGS,
                "start_date": sessions[0],
                "end_date": sessions[-1],
                "benchmark": "none",
            },
            dataset=dataset,
        ).run()
        join_log = next(
            log for log in result.logs
            if log["event_type"] == "SYMBOL_INTRADAY_JOIN"
        )
        self.assertEqual(
            join_log["message"],
            "BTC/USD从2021年1月4日加入回测。",
        )


if __name__ == "__main__":
    unittest.main()
