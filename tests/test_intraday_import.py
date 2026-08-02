from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database.intraday_db as intraday_db
from database import intraday_repository
import services.intraday_import_service as importer


class IntradayImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "intraday.sqlite"
        self.database_patch = patch.object(
            intraday_db,
            "INTRADAY_DATABASE_PATH",
            self.database_path,
        )
        self.database_patch.start()
        intraday_db.init_intraday_database()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temp_dir.cleanup()

    @patch.object(importer, "fetch_stock_bars_page")
    def test_import_pauses_and_resumes_from_saved_page_token(self, fetch_page) -> None:
        fetch_page.side_effect = [
            {
                "data": [{
                    "timestamp": "2020-01-02T14:30:00Z",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 10,
                    "trade_count": 2,
                    "vwap": 100.2,
                }],
                "next_page_token": "page-two",
                "rate_limit": {"remaining": 149},
            },
            {
                "data": [{
                    "timestamp": "2020-01-02T14:31:00Z",
                    "open": 100.5,
                    "high": 102,
                    "low": 100,
                    "close": 101.5,
                    "volume": 12,
                    "trade_count": 3,
                    "vwap": 101.2,
                }],
                "next_page_token": None,
                "rate_limit": {"remaining": 148},
            },
        ]

        progress_updates = []
        paused = importer.import_symbol_history(
            "GLD",
            end="2020-01-03T00:00:00Z",
            max_pages=1,
            progress=progress_updates.append,
        )
        completed = importer.import_symbol_history(
            "GLD",
            end="2020-01-03T00:00:00Z",
        )
        cached = importer.import_symbol_history(
            "GLD",
            end="2020-01-03T00:00:00Z",
        )

        self.assertFalse(paused["complete"])
        self.assertTrue(completed["complete"])
        self.assertEqual(
            fetch_page.call_args_list[1].kwargs["page_token"],
            "page-two",
        )
        self.assertEqual(
            intraday_repository.get_sync_state("GLD")["row_count"],
            2,
        )
        self.assertEqual(completed["job"]["pages_fetched"], 2)
        self.assertTrue(cached["cached_completed_job"])
        self.assertEqual(fetch_page.call_count, 2)
        self.assertEqual(
            progress_updates[0]["page_last_at"],
            "2020-01-02T14:30:00Z",
        )


if __name__ == "__main__":
    unittest.main()
