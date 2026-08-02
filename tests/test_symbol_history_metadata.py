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
        self.assertTrue(symbol["history_start_verified"])
        self.assertEqual(symbol["asset_class"], "crypto")
        self.assertEqual(symbol["quantity_step"], 0.0001)

        repository.mark_symbol_history_start(
            "BTC/USD", "2021-01-01", source="alpaca_crypto",
        )
        self.assertEqual(
            repository.get_symbol("BTC/USD")["history_start_date"],
            "2021-01-01",
        )


if __name__ == "__main__":
    unittest.main()
