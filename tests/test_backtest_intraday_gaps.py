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


if __name__ == "__main__":
    unittest.main()
