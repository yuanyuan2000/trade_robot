from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
import database.intraday_db as intraday_db
import database.intraday_repository as repository


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
