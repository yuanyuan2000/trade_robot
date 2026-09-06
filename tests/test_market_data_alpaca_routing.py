from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

import services.market_data_service as service


class MarketDataAlpacaRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alias = {
            "common_symbol": "GLD",
            "display_name": "GLD",
            "yahoo_symbol": "GLD",
            "twelvedata_symbol": "GLD",
        }

    def test_daily_bar_completion_uses_new_york_close_delay(self) -> None:
        before_delay = datetime(2026, 8, 14, 20, 19, tzinfo=timezone.utc)
        after_delay = datetime(2026, 8, 14, 20, 20, tzinfo=timezone.utc)

        self.assertFalse(
            service._daily_bar_is_complete("2026-08-14", now=before_delay)
        )
        self.assertTrue(
            service._daily_bar_is_complete("2026-08-14", now=after_delay)
        )
        self.assertTrue(
            service._daily_bar_is_complete("2026-08-13", now=before_delay)
        )

    def test_live_iex_snapshot_window_covers_open_until_sip_catches_up(self) -> None:
        self.assertFalse(service._live_iex_snapshot_window(
            now=datetime(2026, 8, 14, 13, 29, tzinfo=timezone.utc)
        ))
        self.assertTrue(service._live_iex_snapshot_window(
            now=datetime(2026, 8, 14, 13, 40, tzinfo=timezone.utc)
        ))
        self.assertFalse(service._live_iex_snapshot_window(
            now=datetime(2026, 8, 14, 20, 20, tzinfo=timezone.utc)
        ))

    @patch.object(service.repository, "log_api_request")
    @patch.object(service.repository, "upsert_daily_prices", return_value=1)
    @patch.object(service, "fetch_stock_snapshots")
    def test_live_iex_snapshot_creates_provisional_current_daily_bar(
        self,
        snapshots,
        upsert,
        _log,
    ) -> None:
        snapshots.return_value = {
            "GLD": {
                "daily_bar": {
                    "timestamp": "2026-08-14T04:00:00Z",
                    "open": 220.0,
                    "high": 222.0,
                    "low": 219.5,
                    "close": 221.5,
                    "volume": 12345,
                }
            }
        }
        results = {"GLD": {"status": "success", "updated_rows": 3}}

        updated = service._merge_live_iex_daily_snapshots(
            {"GLD": "GLD"},
            results,
            now=datetime(2026, 8, 14, 13, 40, tzinfo=timezone.utc),
        )

        self.assertEqual(updated, 1)
        row = upsert.call_args.args[1][0]
        self.assertEqual(row["date"], "2026-08-14")
        self.assertEqual(row["close"], 221.5)
        self.assertFalse(row["is_complete"])
        self.assertEqual(
            upsert.call_args.kwargs["source_timeframe"],
            "snapshot_iex_1Day",
        )
        self.assertEqual(results["GLD"]["source"], "alpaca-iex")
        self.assertEqual(results["GLD"]["updated_rows"], 4)

    @patch.object(service.repository, "log_api_request")
    @patch.object(service, "_fetch_alpaca_daily_prices")
    @patch.object(service, "_ensure_alpaca_capability")
    def test_supported_equity_uses_alpaca_first(
        self,
        capability,
        fetch_alpaca,
        _log,
    ) -> None:
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        fetch_alpaca.return_value = [{"date": "2024-01-02"}]

        rows, provider, provider_symbol = service._fetch_daily_prices_with_fallback(
            self.alias,
            date(2024, 1, 1),
        )

        self.assertEqual(rows, [{"date": "2024-01-02", "is_complete": True}])
        self.assertEqual(provider, "alpaca")
        self.assertEqual(provider_symbol, "GLD")

    @patch.object(service.repository, "log_api_request")
    @patch.object(service, "fetch_yahoo_daily_prices")
    @patch.object(service, "_ensure_alpaca_capability")
    def test_unsupported_symbol_keeps_existing_fallback(
        self,
        capability,
        fetch_yahoo,
        _log,
    ) -> None:
        capability.return_value = {"alpaca_supported": False}
        fetch_yahoo.return_value = [{"date": "2024-01-02"}]

        _, provider, _ = service._fetch_daily_prices_with_fallback(
            self.alias,
            date(2024, 1, 1),
        )

        self.assertEqual(provider, "yahoo")
        fetch_yahoo.assert_called_once()

    def test_usdindex_repairs_missing_yahoo_daily_bar_from_hourly_rows(self) -> None:
        alias = {
            "common_symbol": "USDINDEX",
            "display_name": "USDIndex",
            "yahoo_symbol": "DX-Y.NYB",
            "twelvedata_symbol": None,
        }
        derived = [{
            "date": "2026-08-28",
            "open": 99.11,
            "high": 99.73,
            "low": 99.10,
            "close": 99.70,
            "volume": 0,
            "source_timeframe": "60MinDerived",
        }]
        with (
            patch.object(service.repository, "resolve_symbol_alias", return_value=alias),
            patch.object(service, "_fetch_daily_prices_with_fallback", return_value=([], "yahoo", "DX-Y.NYB")),
            patch.object(service.repository, "upsert_symbol"),
            patch.object(service.repository, "upsert_daily_prices", side_effect=[2, 1]) as upsert,
            patch.object(service, "latest_completed_session_dates", return_value=["2026-08-27", "2026-08-28"]),
            patch.object(service, "assess_daily_history", return_value={
                "missing_sessions": ["2026-08-28"],
                "incomplete_sessions": [],
            }),
            patch.object(service, "fetch_yahoo_hourly_derived_daily_prices", return_value=derived) as hourly,
        ):
            result = service.refresh_symbol_daily_history(
                "USDINDEX",
                start_date="2026-08-28",
            )

        hourly.assert_called_once_with("DX-Y.NYB", ["2026-08-28"])
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(result["updated_rows"], 3)
        self.assertEqual(
            result["hourly_daily_repair"]["repaired_dates"],
            ["2026-08-28"],
        )

    @patch.object(service, "update_full_market_data")
    def test_query_checkbox_requests_intraday_initialization(self, update) -> None:
        update.return_value = {"ok": True}

        result = service.get_market_data("GLD", include_intraday=True)

        self.assertEqual(result, {"ok": True})
        update.assert_called_once_with("GLD", initialize_intraday=True)

    def test_verified_late_inception_counts_as_initialized_history(
        self,
    ) -> None:
        sync_state = {
            "row_count": 2_600_000,
            "earliest_minute_at": "2021-01-01T06:00:00Z",
            "latest_complete_minute_at": "2026-07-31T17:53:00Z",
            "minute_history_start_date": "2021-01-01",
            "minute_history_start_verified": True,
        }

        self.assertTrue(
            service._has_initialized_intraday_history("BTC/USD", sync_state)
        )

    def test_unverified_late_start_does_not_hide_missing_history(
        self,
    ) -> None:
        sync_state = {
            "row_count": 2_600_000,
            "earliest_minute_at": "2021-01-01T06:00:00Z",
            "latest_complete_minute_at": "2026-07-31T17:53:00Z",
            "minute_history_start_date": "2021-01-01",
            "minute_history_start_verified": False,
        }

        self.assertFalse(
            service._has_initialized_intraday_history("BTC/USD", sync_state)
        )

    @patch.object(service, "_merge_live_iex_daily_snapshots", return_value=0)
    @patch.object(service.repository, "log_api_request")
    @patch.object(service, "_fetch_alpaca_daily_prices")
    @patch.object(service, "derive_daily_prices_from_minutes")
    @patch.object(service, "import_symbol_history")
    @patch.object(service.intraday_repository, "get_sync_state")
    @patch.object(service, "_ensure_alpaca_capability")
    def test_overview_refreshes_daily_without_touching_initialized_minutes(
        self,
        capability,
        get_sync_state,
        import_history,
        derive_daily,
        fetch_daily,
        _log,
        merge_live,
    ) -> None:
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        get_sync_state.return_value = {
            "status": "success",
            "row_count": 100,
            "earliest_minute_at": "2020-01-02T14:30:00Z",
            "latest_complete_minute_at": "2024-01-05T20:00:00Z",
        }
        fetch_daily.return_value = [{"date": "2024-01-05"}]
        results = {"GLD": {"status": "pending"}}

        with (
            patch.object(service.repository, "upsert_symbol"),
            patch.object(
                service.repository,
                "upsert_daily_prices",
                return_value=2,
            ),
        ):
            pending = service._sync_overview_daily_from_alpaca(
                [self.alias],
                date(2024, 1, 1),
                results,
            )

        self.assertEqual(pending, [])
        self.assertEqual(results["GLD"]["source"], "alpaca")
        self.assertEqual(results["GLD"]["updated_rows"], 2)
        fetch_daily.assert_called_once_with(
            "GLD",
            start_date=date(2024, 1, 1),
        )
        import_history.assert_not_called()
        derive_daily.assert_not_called()
        merge_live.assert_called_once_with({"GLD": "GLD"}, results)

    @patch.object(service, "_merge_live_iex_daily_snapshots", return_value=0)
    @patch.object(service, "_fetch_alpaca_daily_prices")
    @patch.object(service, "_ensure_alpaca_capability")
    def test_overview_skips_history_api_when_required_sessions_are_complete(
        self,
        capability,
        fetch_daily,
        merge_live,
    ) -> None:
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        results = {"GLD": {"status": "pending"}}

        pending = service._sync_overview_daily_from_alpaca(
            [self.alias],
            date(2024, 1, 1),
            results,
            history_audits={"GLD": {"complete": True}},
        )

        self.assertEqual(pending, [])
        self.assertEqual(results["GLD"]["source"], "database")
        self.assertEqual(results["GLD"]["updated_rows"], 0)
        fetch_daily.assert_not_called()
        merge_live.assert_called_once_with({"GLD": "GLD"}, results)

    @patch.object(service.repository, "get_symbol")
    @patch.object(service.repository, "get_daily_prices")
    @patch.object(service.repository, "upsert_daily_prices")
    @patch.object(service.repository, "upsert_symbol")
    @patch.object(service, "_has_history_start")
    @patch.object(service, "_overview_sync_start_date")
    @patch.object(service, "_fetch_alpaca_daily_prices")
    @patch.object(service, "import_symbol_history")
    @patch.object(service.intraday_repository, "get_sync_state")
    @patch.object(service, "_ensure_alpaca_capability")
    @patch.object(service.repository, "resolve_symbol_alias")
    def test_manual_daily_only_does_not_touch_initialized_minutes(
        self,
        resolve_alias,
        capability,
        get_sync_state,
        import_history,
        fetch_daily,
        sync_start,
        has_history_start,
        _upsert_symbol,
        _upsert_daily,
        get_daily_prices,
        get_symbol,
    ) -> None:
        resolve_alias.return_value = self.alias
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        get_sync_state.return_value = {
            "status": "success",
            "row_count": 100,
            "earliest_minute_at": "2020-01-02T14:30:00Z",
            "latest_complete_minute_at": "2024-01-05T20:00:00Z",
        }
        sync_start.return_value = date(2024, 1, 1)
        fetch_daily.return_value = [{"date": "2024-01-05"}]
        has_history_start.return_value = True
        get_daily_prices.return_value = [{"date": "2024-01-05"}]
        get_symbol.return_value = {"alpaca_supported": True}

        with patch.object(
            service,
            "_manual_integrity",
            return_value={"complete": True},
        ):
            service.update_full_market_data("GLD", initialize_intraday=False)

        fetch_daily.assert_called_once_with(
            "GLD",
            start_date=date(2024, 1, 1),
        )
        import_history.assert_not_called()
        get_sync_state.assert_not_called()

    @patch.object(service, "_merge_live_iex_daily_snapshots", return_value=0)
    @patch.object(service, "refresh_symbol_daily_history")
    @patch.object(
        service.repository,
        "get_legacy_session_daily_start",
        return_value="2021-01-04",
    )
    @patch.object(service, "_ensure_alpaca_capability")
    def test_overview_repairs_legacy_crypto_native_daily_even_when_sessions_complete(
        self,
        capability,
        _legacy_start,
        refresh_daily,
        _merge_live,
    ) -> None:
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "BTC/USD",
        }
        refresh_daily.return_value = {
            "updated_rows": 100,
            "source": "yahoo",
        }
        alias = {
            "common_symbol": "BTC/USD",
            "display_name": "BTC/USD",
            "yahoo_symbol": "BTC-USD",
            "twelvedata_symbol": "BTC/USD",
        }
        results = {"BTC/USD": {"status": "pending"}}

        pending = service._sync_overview_daily_from_alpaca(
            [alias],
            date(2026, 8, 1),
            results,
            history_audits={"BTC/USD": {"complete": True}},
        )

        self.assertEqual(pending, [])
        refresh_daily.assert_called_once_with(
            "BTC/USD",
            start_date=date(2021, 1, 4),
            priority=service.PRIORITY_OVERVIEW,
        )
        self.assertEqual(results["BTC/USD"]["source"], "yahoo")

    @patch.object(service.repository, "get_symbol")
    @patch.object(service.repository, "get_daily_prices")
    @patch.object(service, "derive_daily_prices_from_minutes")
    @patch.object(service, "repair_sparse_regular_session_minutes")
    @patch.object(service, "import_symbol_history")
    @patch.object(service.intraday_repository, "get_sync_state")
    @patch.object(service, "_ensure_alpaca_capability")
    @patch.object(service.repository, "resolve_symbol_alias")
    def test_manual_intraday_initializes_from_2020_when_history_is_missing(
        self,
        resolve_alias,
        capability,
        get_sync_state,
        import_history,
        repair_minutes,
        derive_daily,
        get_daily_prices,
        _get_symbol,
    ) -> None:
        resolve_alias.return_value = self.alias
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        get_sync_state.return_value = {
            "status": "not_initialized",
            "row_count": 0,
        }
        import_history.return_value = {
            "sync_state": {
                "status": "success",
                "row_count": 100,
            }
        }
        repair_minutes.return_value = {
            "synthetic_rows_added": 12,
            "sessions_repaired": 1,
        }
        derive_daily.return_value = {"updated_rows": 10}
        get_daily_prices.return_value = [{"date": "2020-01-02"}]

        progress_updates = []
        with patch.object(
            service,
            "_manual_integrity",
            return_value={"complete": True},
        ):
            result = service.update_full_market_data(
                "GLD",
                initialize_intraday=True,
                progress_callback=progress_updates.append,
            )

        self.assertEqual(import_history.call_args.args, ("GLD",))
        self.assertEqual(
            import_history.call_args.kwargs["start"],
            service.FULL_HISTORY_START_DATE,
        )
        self.assertTrue(callable(import_history.call_args.kwargs["progress"]))
        repair_minutes.assert_called_once_with("GLD", completed_through=None)
        derive_daily.assert_called_once_with("GLD", start_at=None)
        self.assertEqual(result["intraday_repair"]["synthetic_rows_added"], 12)
        self.assertEqual(progress_updates[0]["stage"], "checking")
        self.assertEqual(progress_updates[-1]["stage"], "completed")

    @patch.object(service.repository, "get_symbol")
    @patch.object(service.repository, "get_daily_prices")
    @patch.object(service.repository, "upsert_daily_prices")
    @patch.object(service.repository, "upsert_symbol")
    @patch.object(service, "_has_history_start")
    @patch.object(service, "_overview_sync_start_date")
    @patch.object(service, "_fetch_daily_prices_with_fallback")
    @patch.object(service.intraday_repository, "get_sync_state")
    @patch.object(service, "_ensure_alpaca_capability")
    @patch.object(service.repository, "resolve_symbol_alias")
    def test_manual_daily_fallback_always_refreshes_recent_window(
        self,
        resolve_alias,
        capability,
        get_sync_state,
        fetch_fallback,
        sync_start,
        has_history_start,
        _upsert_symbol,
        _upsert_daily,
        get_daily_prices,
        get_symbol,
    ) -> None:
        resolve_alias.return_value = self.alias
        capability.return_value = {"alpaca_supported": False}
        get_sync_state.return_value = {
            "status": "not_initialized",
            "row_count": 0,
        }
        sync_start.return_value = date(2024, 1, 1)
        fetch_fallback.return_value = (
            [{"date": "2024-01-05"}],
            "yahoo",
            "GLD",
        )
        has_history_start.return_value = True
        get_daily_prices.return_value = [{"date": "2024-01-05"}]
        get_symbol.return_value = {"alpaca_supported": False}

        with patch.object(
            service,
            "_manual_integrity",
            return_value={"complete": True},
        ):
            service.update_full_market_data("GLD", initialize_intraday=False)

        fetch_fallback.assert_called_once_with(
            self.alias,
            date(2024, 1, 1),
        )

    @patch.object(service, "_merge_live_iex_daily_snapshots", return_value=0)
    @patch.object(service.repository, "upsert_daily_prices")
    @patch.object(service.repository, "upsert_symbol")
    @patch.object(service.repository, "log_api_request")
    @patch.object(service, "_fetch_alpaca_daily_prices")
    @patch.object(service, "import_symbol_history")
    @patch.object(service.intraday_repository, "get_sync_state")
    @patch.object(service, "_ensure_alpaca_capability")
    def test_overview_does_not_start_full_minute_import(
        self,
        capability,
        get_sync_state,
        import_history,
        fetch_daily,
        _log,
        _upsert_symbol,
        _upsert_daily,
        merge_live,
    ) -> None:
        capability.return_value = {
            "alpaca_supported": True,
            "alpaca_symbol": "GLD",
        }
        get_sync_state.return_value = {
            "status": "not_initialized",
            "row_count": 0,
        }
        fetch_daily.return_value = [{"date": "2024-01-02"}]
        results = {"GLD": {"status": "pending"}}

        service._sync_overview_daily_from_alpaca(
            [self.alias],
            date(2024, 1, 1),
            results,
        )

        self.assertEqual(results["GLD"]["source"], "alpaca")
        import_history.assert_not_called()
        merge_live.assert_called_once_with({"GLD": "GLD"}, results)

    @patch.object(service, "_sync_overview_daily_from_twelve_data")
    @patch.object(service, "_sync_overview_daily_from_yahoo")
    @patch.object(service, "_sync_overview_daily_from_alpaca")
    @patch.object(service, "_overview_sync_start_date")
    @patch.object(service.repository, "list_overview_symbols")
    def test_automatic_sync_is_limited_to_overview_symbols(
        self,
        list_overview,
        sync_start,
        sync_alpaca,
        sync_yahoo,
        sync_twelve,
    ) -> None:
        list_overview.return_value = [self.alias]
        sync_start.return_value = date(2024, 1, 1)
        sync_alpaca.return_value = []
        sync_yahoo.return_value = []

        service.sync_market_overview_daily_prices()

        list_overview.assert_called_once_with()
        self.assertEqual(sync_alpaca.call_args.args[0], [self.alias])
        sync_yahoo.assert_called_once()
        sync_twelve.assert_called_once()

    @patch.object(service, "_sync_overview_daily_from_twelve_data")
    @patch.object(service, "_sync_overview_daily_from_yahoo", return_value=[])
    @patch.object(service, "_sync_overview_daily_from_alpaca")
    @patch.object(service, "_overview_sync_start_date", return_value=date(2024, 1, 1))
    @patch.object(service.repository, "list_overview_symbols")
    @patch.object(service, "latest_completed_session_dates")
    def test_overview_reports_unverified_when_calendar_validation_fails(
        self,
        completed_sessions,
        list_overview,
        _sync_start,
        sync_alpaca,
        _sync_yahoo,
        _sync_twelve,
    ) -> None:
        completed_sessions.side_effect = RuntimeError("calendar unavailable")
        list_overview.return_value = [self.alias]

        def mark_success(_aliases, _start, results, **_kwargs):
            results["GLD"].update({
                "source": "alpaca",
                "status": "success",
                "updated_rows": 1,
            })
            return []

        sync_alpaca.side_effect = mark_success

        result = service.sync_market_overview_daily_prices()

        self.assertEqual(result["history_validation_error"], "calendar unavailable")
        self.assertEqual(result["items"][0]["status"], "unverified")
        self.assertIsNone(result["items"][0]["history_complete"])


if __name__ == "__main__":
    unittest.main()
