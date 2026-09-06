from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
import database.intraday_db as intraday_db
import database.intraday_repository as repository
import services.intraday_bar_service as bar_service


class IntradayStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "intraday.sqlite"
        self.backup_root = Path(self.temp_dir.name) / "data"
        self.backup_root.mkdir()
        self.patchers = [
            patch.object(intraday_db, "INTRADAY_DATABASE_PATH", self.database_path),
            patch.object(main_db, "INTRADAY_DATABASE_PATH", self.database_path),
            patch.object(main_db, "DATA_DIR", self.backup_root),
        ]
        for patcher in self.patchers:
            patcher.start()
        intraday_db.init_intraday_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_scaled_prices_round_trip_and_upsert_is_idempotent(self) -> None:
        row = {
            "timestamp": "2020-01-02T14:30:00Z",
            "open": "143.860001",
            "high": "143.900002",
            "low": "143.800003",
            "close": "143.890004",
            "volume": 100,
            "trade_count": 5,
            "vwap": "143.870005",
        }
        repository.upsert_minute_bars("GLD", [row])
        repository.upsert_minute_bars("GLD", [{**row, "close": "144.000006"}])
        repository.mark_sync_result("GLD", "success")

        rows = repository.get_minute_bars("GLD")
        state = repository.get_sync_state("GLD")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 144.000006)
        self.assertEqual(rows[0]["timestamp"], "2020-01-02T14:30:00Z")
        self.assertEqual(state["row_count"], 1)
        self.assertEqual(state["status"], "success")

    def test_sparse_session_repair_supports_signal_and_simulated_execution(self) -> None:
        session_open = repository.iso_to_epoch_minute("2024-01-03T14:30:00Z")
        session_close = session_open + 390
        first_trade = session_open + 92
        later_trade = session_open + 110
        repository.upsert_minute_bars(
            "MAGS",
            [
                {
                    "minute_utc": first_trade,
                    "open": 25,
                    "high": 25,
                    "low": 25,
                    "close": 25,
                    "volume": 100,
                },
                {
                    "minute_utc": later_trade,
                    "open": 26,
                    "high": 26,
                    "low": 26,
                    "close": 26,
                    "volume": 200,
                },
            ],
        )
        repository.mark_sync_result("MAGS", "success")
        sessions = [{
            "trading_date": "2024-01-03",
            "open_minute_utc": session_open,
            "close_minute_utc": session_close,
            "is_early_close": False,
        }]
        daily = [{
            "date": "2024-01-02", "open": 24, "high": 24,
            "low": 24, "close": 24, "volume": 1,
        }]

        with (
            patch(
                "services.backtest.market_calendar.ensure_market_sessions",
                return_value=sessions,
            ),
            patch.object(bar_service.repository, "get_daily_prices", return_value=daily),
        ):
            repaired = bar_service.repair_sparse_regular_session_minutes(
                "MAGS",
                completed_through="2024-01-04T00:00:00Z",
            )

        self.assertEqual(repaired["synthetic_rows_added"], 388)
        leading = repository.get_minute_bars_at("MAGS", [session_open])[session_open]
        self.assertTrue(leading["is_synthetic"])
        self.assertEqual(leading["close"], 24)
        resolved = repository.resolve_minute_event_gaps(
            "MAGS",
            [{
                "target_minute": session_open + 20,
                "open_minute": session_open,
                "close_minute": session_close,
            }],
        )[session_open + 20]
        self.assertEqual(resolved["signal_minute"], session_open + 19)
        self.assertEqual(resolved["fill_minute"], session_open + 20)

        with (
            patch(
                "services.backtest.market_calendar.ensure_market_sessions",
                return_value=sessions,
            ),
            patch.object(bar_service.repository, "get_daily_prices", return_value=daily),
        ):
            repeated = bar_service.repair_sparse_regular_session_minutes(
                "MAGS",
                completed_through="2024-01-04T00:00:00Z",
            )
        self.assertEqual(repeated["synthetic_rows_added"], 0)

    @patch.object(bar_service.repository, "upsert_daily_prices")
    @patch.object(bar_service.intraday_repository, "iter_minute_bars")
    def test_daily_derivation_ignores_synthetic_minutes(
        self,
        iter_rows,
        upsert_daily,
    ) -> None:
        synthetic = {
            "minute_utc": repository.iso_to_epoch_minute("2024-01-03T14:30:00Z"),
            "open": 90,
            "high": 90,
            "low": 90,
            "close": 90,
            "volume": 0,
            "is_synthetic": True,
        }
        actual = {
            "minute_utc": repository.iso_to_epoch_minute("2024-01-03T16:00:00Z"),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 10,
            "is_synthetic": False,
        }
        iter_rows.return_value = iter([synthetic, actual])
        upsert_daily.return_value = 1

        bar_service.derive_daily_prices_from_minutes("MAGS")

        row = upsert_daily.call_args.args[1][0]
        self.assertEqual(row["open"], 100)
        self.assertEqual(row["low"], 99)
        self.assertEqual(row["volume"], 10)

    def test_daily_and_minute_history_starts_are_separate(self) -> None:
        instrument = repository.get_instrument("BTC/USD")
        if instrument is None:
            repository.upsert_instrument("BTC/USD", asset_class="crypto")
            instrument = repository.get_instrument("BTC/USD")

        self.assertEqual(
            instrument["minute_history_start_date"],
            "2021-01-01",
        )
        self.assertEqual(
            instrument["minute_history_start_source"],
            "alpaca_crypto",
        )
        self.assertTrue(instrument["minute_history_start_verified"])

    def test_monthly_fingerprint_is_compact_and_stable(self) -> None:
        repository.upsert_minute_bars(
            "SPY",
            [
                {
                    "timestamp": f"2020-01-02T14:3{minute}:00Z",
                    "open": 320 + minute,
                    "high": 321 + minute,
                    "low": 319 + minute,
                    "close": 320.5 + minute,
                    "volume": 100 + minute,
                }
                for minute in range(2)
            ],
        )

        first = repository.recompute_monthly_fingerprint("SPY", "2020-01")
        second = repository.recompute_monthly_fingerprint("SPY", "2020-01")

        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(len(bytes.fromhex(first["digest"])), 16)
        self.assertEqual(first["row_count"], 2)

    def test_intraday_backup_is_selectable_and_valid(self) -> None:
        repository.upsert_minute_bars(
            "NVDA",
            [{
                "timestamp": "2020-01-02T14:30:00Z",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 10,
            }],
        )

        backups = main_db.backup_databases(["intraday"])

        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["target"], "intraday")
        self.assertEqual(backups[0]["quick_check"], "ok")
        self.assertTrue(Path(backups[0]["path"]).exists())

    def test_intraday_database_uses_requested_page_size(self) -> None:
        info = intraday_db.intraday_database_info()
        self.assertEqual(info["page_size"], intraday_db.INTRADAY_PAGE_SIZE)

    def test_calendar_refresh_removes_dates_no_longer_returned(self) -> None:
        repository.upsert_market_sessions(
            [
                {
                    "trading_date": "2024-07-03",
                    "open_minute_utc": 100,
                    "close_minute_utc": 200,
                    "is_early_close": True,
                }
            ],
            coverage_start="2024-07-01",
            coverage_end="2024-07-05",
        )
        self.assertEqual(
            len(repository.get_market_sessions("2024-07-01", "2024-07-05")),
            1,
        )

        repository.upsert_market_sessions(
            [],
            coverage_start="2024-07-01",
            coverage_end="2024-07-05",
        )

        self.assertEqual(
            repository.get_market_sessions("2024-07-01", "2024-07-05"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
