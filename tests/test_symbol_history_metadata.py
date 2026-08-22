from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.db as main_db
from database import repository


class SymbolHistoryMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.patcher = patch.object(main_db, "DATABASE_PATH", self.database_path)
        self.patcher.start()
        main_db.init_database()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_history_start_only_moves_earlier_and_crypto_metadata_is_stable(self) -> None:
        repository.mark_symbol_history_start(
            "BTC/USD", "2021-01-05", source="alpaca_crypto",
            asset_class="crypto", quantity_step=0.0001,
        )
        repository.mark_symbol_history_start(
            "BTC/USD", "2021-01-10", source="alpaca_crypto",
        )
        repository.upsert_symbol("BTC/USD")

        symbol = repository.get_symbol("BTC/USD")
        self.assertEqual(symbol["history_start_date"], "2021-01-05")
        self.assertEqual(symbol["daily_history_start_date"], "2021-01-05")
        self.assertTrue(symbol["history_start_verified"])
        self.assertTrue(symbol["daily_history_start_verified"])
        self.assertEqual(symbol["asset_class"], "crypto")
        self.assertEqual(symbol["quantity_step"], 0.0001)

        repository.mark_symbol_history_start(
            "BTC/USD", "2021-01-01", source="alpaca_crypto",
        )
        self.assertEqual(
            repository.get_symbol("BTC/USD")["history_start_date"],
            "2021-01-01",
        )

    def test_overview_snapshot_exposes_latest_bar_provenance_and_completion(self) -> None:
        repository.upsert_symbol("SPY")
        repository.upsert_daily_prices(
            "SPY",
            [
                {"date": "2026-08-13", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10, "is_complete": 1, "price_basis": "raw"},
                {"date": "2026-08-14", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 12, "is_complete": 0, "price_basis": "raw"},
            ],
            source_provider="alpaca",
            source_timeframe="1Day",
        )

        snapshot = repository.get_symbol_price_snapshot("SPY")

        self.assertFalse(snapshot["latest_price_is_complete"])
        self.assertTrue(snapshot["latest_price_is_provisional"])
        self.assertEqual(snapshot["latest_price_basis"], "raw")
        self.assertEqual(snapshot["latest_price_source"], "alpaca")
        self.assertEqual(snapshot["latest_price_timeframe"], "1Day")

    def test_migration_copies_legacy_crypto_session_rows_without_deleting_native(self) -> None:
        repository.upsert_symbol("BTC/USD", {"asset_class": "crypto"})
        repository.upsert_daily_prices(
            "BTC/USD",
            [{
                "date": "2021-01-04",
                "open": 30000,
                "high": 31000,
                "low": 29500,
                "close": 30500,
                "volume": 100,
                "price_basis": "raw",
            }],
            source_provider="alpaca_crypto",
            source_timeframe="nyse_session_derived_1m",
        )

        with main_db.get_connection() as conn:
            main_db.migrate_database(conn)
            main_db.migrate_database(conn)

        native = repository.get_daily_prices("BTC/USD", include_metadata=True)
        strategy = repository.get_daily_price_series(
            "BTC/USD",
            "US_EQUITY_SESSION",
            include_metadata=True,
        )
        self.assertEqual(len(native), 1)
        self.assertEqual(len(strategy), 1)
        self.assertEqual(strategy[0]["close"], native[0]["close"])
        self.assertEqual(
            strategy[0]["source_timeframe"],
            "nyse_session_derived_1m",
        )
        self.assertEqual(
            repository.get_legacy_session_daily_start("BTC/USD"),
            "2021-01-04",
        )


if __name__ == "__main__":
    unittest.main()
