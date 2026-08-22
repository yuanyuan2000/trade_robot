from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import database.db as main_db
from database import repository
from services.market_data_integrity import (
    MarketDataIntegrityError,
    assess_daily_history,
)
from services.market_data_request_coordinator import (
    MarketDataRequestCoordinator,
    PRIORITY_FORMAL_DECISION,
    PRIORITY_OVERVIEW,
)
from services.realtime_history_service import prepare_strategy_history


def _daily_row(day: str, close: float, *, complete: bool = True) -> dict:
    return {
        "date": day,
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1000,
        "is_complete": complete,
    }


class MarketDataIntegrityTests(unittest.TestCase):
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

    def test_assessment_detects_missing_and_provisional_sessions(self) -> None:
        repository.upsert_symbol("SLV")
        repository.upsert_daily_prices(
            "SLV",
            [
                _daily_row("2026-08-10", 31.0),
                _daily_row("2026-08-11", 31.5, complete=False),
            ],
            source_provider="alpaca",
            source_timeframe="1Day",
        )

        audit = assess_daily_history(
            "SLV",
            ["2026-08-10", "2026-08-11", "2026-08-12"],
        )

        self.assertFalse(audit["complete"])
        self.assertEqual(audit["incomplete_sessions"], ["2026-08-11"])
        self.assertEqual(audit["missing_sessions"], ["2026-08-12"])
        self.assertEqual(audit["repair_start_date"], "2026-08-11")

    def test_formal_history_repairs_candidate_not_in_market_overview(self) -> None:
        repository.upsert_symbol("SLV")
        repository.upsert_daily_prices(
            "SLV",
            [_daily_row("2026-08-10", 31.0)],
            source_provider="alpaca",
            source_timeframe="1Day",
        )
        strategy = {
            "design_mode": "visual",
            "selection_mode": "single",
            "definition": {
                "symbols": [{"symbol": "SLV"}],
                "rules": [{"enabled": True, "condition": "true"}],
            },
        }
        refresh_calls = []

        def refresh(symbol: str, start_date: str) -> None:
            refresh_calls.append((symbol, start_date))
            repository.upsert_daily_prices(
                symbol,
                [_daily_row("2026-08-11", 31.8)],
                source_provider="alpaca",
                source_timeframe="1Day",
            )

        with patch(
            "services.realtime_history_service.required_completed_sessions",
            return_value=["2026-08-10", "2026-08-11"],
        ):
            snapshot = prepare_strategy_history(
                strategy,
                trading_date="2026-08-12",
                refresh=refresh,
            )

        self.assertEqual(refresh_calls, [("SLV", "2026-08-11")])
        self.assertTrue(snapshot["symbols"]["SLV"]["complete"])
        self.assertEqual(snapshot["daily"]["SLV"][-1]["date"], "2026-08-11")
        self.assertFalse(repository.get_symbol("SLV")["show_in_overview"])

    def test_formal_history_fails_closed_when_repair_does_not_fill_gap(self) -> None:
        repository.upsert_symbol("SLV")
        strategy = {
            "design_mode": "visual",
            "selection_mode": "single",
            "definition": {
                "symbols": [{"symbol": "SLV"}],
                "rules": [{"enabled": True, "condition": "true"}],
            },
        }
        with (
            patch(
                "services.realtime_history_service.required_completed_sessions",
                return_value=["2026-08-10", "2026-08-11"],
            ),
            self.assertRaises(MarketDataIntegrityError) as raised,
        ):
            prepare_strategy_history(
                strategy,
                trading_date="2026-08-12",
                refresh=lambda _symbol, _start: None,
            )

        self.assertEqual(raised.exception.code, "HISTORY_STALE")
        self.assertIn("SLV", str(raised.exception))

    def test_crypto_candidate_uses_named_us_session_history_in_validation(self) -> None:
        strategy = {
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": [
                    {"symbol": "SPY"},
                    {"symbol": "BTC/USD"},
                ],
                "rules": [],
                "competition": {
                    "eligibility": "true",
                    "score": "price",
                },
            },
        }
        repository.upsert_symbol("SPY")
        repository.upsert_daily_prices(
            "SPY",
            [_daily_row("2026-08-10", 630), _daily_row("2026-08-11", 632)],
            source_provider="alpaca",
            source_timeframe="1Day",
        )
        repository.upsert_symbol("BTC/USD", {"asset_class": "crypto"})
        repository.upsert_daily_prices(
            "BTC/USD",
            [
                _daily_row("2026-08-09", 118000),
                _daily_row("2026-08-10", 119000),
                _daily_row("2026-08-11", 120000),
            ],
            source_provider="alpaca_crypto",
            source_timeframe="1Day",
        )
        repository.upsert_daily_price_series(
            "BTC/USD",
            "US_EQUITY_SESSION",
            [
                _daily_row("2026-08-10", 119100),
                _daily_row("2026-08-11", 120100),
            ],
            source_provider="alpaca_crypto",
            source_timeframe="nyse_session_derived_1m",
        )
        with patch(
            "services.realtime_history_service.required_completed_sessions",
            return_value=["2026-08-10", "2026-08-11"],
        ):
            snapshot = prepare_strategy_history(
                strategy,
                trading_date="2026-08-12",
            )

        self.assertEqual(set(snapshot["symbols"]), {"SPY", "BTC/USD"})
        self.assertEqual(
            [row["date"] for row in snapshot["daily"]["BTC/USD"]],
            ["2026-08-10", "2026-08-11"],
        )
        self.assertEqual(snapshot["daily"]["BTC/USD"][-1]["close"], 120100)
        self.assertEqual(
            repository.get_daily_prices("BTC/USD")[-1]["close"],
            120000,
        )


class MarketDataRequestCoordinatorTests(unittest.TestCase):
    def test_same_key_concurrent_requests_share_one_result(self) -> None:
        coordinator = MarketDataRequestCoordinator()
        callback_started = threading.Event()
        release_callback = threading.Event()
        callback_calls = []
        results = []

        def callback() -> dict:
            callback_calls.append("called")
            callback_started.set()
            release_callback.wait(timeout=2)
            return {"value": 42}

        first = threading.Thread(target=lambda: results.append(coordinator.run(
            ("daily-history", "SLV"),
            priority=PRIORITY_OVERVIEW,
            callback=callback,
        )))
        second = threading.Thread(target=lambda: results.append(coordinator.run(
            ("daily-history", "SLV"),
            priority=PRIORITY_FORMAL_DECISION,
            callback=callback,
        )))
        first.start()
        self.assertTrue(callback_started.wait(timeout=1))
        second.start()
        release_callback.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(callback_calls, ["called"])
        self.assertEqual(results, [{"value": 42}, {"value": 42}])

    def test_formal_request_uses_reserved_slot_during_overview_refresh(self) -> None:
        coordinator = MarketDataRequestCoordinator(
            max_active=2,
            max_background_active=1,
        )
        overview_started = threading.Event()
        release_overview = threading.Event()
        formal_started = threading.Event()

        def overview_callback() -> str:
            overview_started.set()
            release_overview.wait(timeout=2)
            return "overview"

        overview = threading.Thread(target=lambda: coordinator.run(
            ("overview", "batch"),
            priority=PRIORITY_OVERVIEW,
            callback=overview_callback,
        ))
        formal = threading.Thread(target=lambda: coordinator.run(
            ("formal", "SLV"),
            priority=PRIORITY_FORMAL_DECISION,
            callback=lambda: formal_started.set(),
        ))
        overview.start()
        self.assertTrue(overview_started.wait(timeout=1))
        formal.start()

        self.assertTrue(formal_started.wait(timeout=1))
        release_overview.set()
        overview.join(timeout=2)
        formal.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
