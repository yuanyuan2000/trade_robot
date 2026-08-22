from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import services.intraday_bar_service as service


def minute_row(timestamp: str, price: float, volume: int = 1) -> dict:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return {
        "minute_utc": int(parsed.astimezone(timezone.utc).timestamp()) // 60,
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price + 0.5,
        "volume": volume,
    }


class IntradayBarServiceTests(unittest.TestCase):
    def test_bar_spec_keeps_minute_and_month_codes_distinct(self) -> None:
        self.assertEqual(service.parse_bar_spec("1m")["unit"], "minute")
        self.assertEqual(service.parse_bar_spec("1M")["unit"], "month")
        self.assertEqual(service.parse_bar_spec("8D")["size"], 8)
        self.assertEqual(service.parse_bar_spec("10m")["size"], 10)

    def test_intraday_aggregation_is_session_anchored_and_excludes_extended_hours(self) -> None:
        rows = [
            minute_row("2024-01-02T14:29:00Z", 99),   # 09:29 ET
            minute_row("2024-01-02T14:30:00Z", 100),  # 09:30 ET
            minute_row("2024-01-02T18:29:00Z", 110),  # 13:29 ET
            minute_row("2024-01-02T18:30:00Z", 120),  # 13:30 ET
            minute_row("2024-01-02T21:00:00Z", 130),  # 16:00 ET
        ]

        bars = service.aggregate_intraday_rows(rows, 240)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2024-01-02 09:30")
        self.assertEqual(bars[0]["open"], 100)
        self.assertEqual(bars[0]["close"], 110.5)
        self.assertEqual(bars[1]["date"], "2024-01-02 13:30")
        self.assertEqual(bars[1]["open"], 120)

    def test_live_intraday_bucket_is_marked_provisional_until_settled(self) -> None:
        rows = [minute_row("2026-08-14T13:30:00Z", 100)]  # 09:30 EDT

        live = service.aggregate_intraday_rows(
            rows,
            15,
            now=datetime(2026, 8, 14, 13, 40, tzinfo=timezone.utc),
        )
        settled = service.aggregate_intraday_rows(
            rows,
            15,
            now=datetime(2026, 8, 14, 13, 46, tzinfo=timezone.utc),
        )

        self.assertFalse(live[0]["is_complete"])
        self.assertTrue(settled[0]["is_complete"])

    @patch.object(service, "market_sessions")
    def test_intraday_filter_removes_holiday_and_minutes_after_early_close(
        self,
        sessions,
    ) -> None:
        open_minute = minute_row("2024-07-03T13:30:00Z", 100)["minute_utc"]
        close_minute = minute_row("2024-07-03T17:00:00Z", 101)["minute_utc"]
        sessions.return_value = [{
            "trading_date": "2024-07-03",
            "open_minute_utc": open_minute,
            "close_minute_utc": close_minute,
            "is_early_close": True,
        }]
        rows = [
            minute_row("2024-07-03T13:30:00Z", 100),
            minute_row("2024-07-03T16:59:00Z", 101),
            minute_row("2024-07-03T17:00:00Z", 102),
            minute_row("2024-07-04T13:30:00Z", 103),
        ]

        filtered = service.filter_minute_rows_for_us_market(rows)

        self.assertEqual(
            [row["minute_utc"] for row in filtered],
            [rows[0]["minute_utc"], rows[1]["minute_utc"]],
        )

    @patch.object(service.repository, "upsert_daily_prices")
    @patch.object(service.intraday_repository, "iter_minute_bars")
    def test_daily_prices_are_derived_from_regular_session_only(
        self,
        iter_rows,
        upsert_daily,
    ) -> None:
        iter_rows.return_value = iter(
            [
                minute_row("2024-01-02T13:00:00Z", 90),
                minute_row("2024-01-02T14:30:00Z", 100, 10),
                minute_row("2024-01-02T20:59:00Z", 105, 20),
                minute_row("2024-01-03T14:30:00Z", 110, 30),
            ]
        )
        upsert_daily.return_value = 2

        result = service.derive_daily_prices_from_minutes("SPY")

        rows = upsert_daily.call_args.args[1]
        self.assertEqual(result["updated_rows"], 2)
        self.assertEqual(rows[0]["date"], "2024-01-02")
        self.assertEqual(rows[0]["open"], 100)
        self.assertEqual(rows[0]["close"], 105.5)
        self.assertEqual(rows[0]["volume"], 30)
        self.assertEqual(rows[0]["source_timeframe"], "derived_1m")

        service.derive_daily_prices_from_minutes(
            "SPY",
            start_at="2024-01-03T00:00:00Z",
        )
        self.assertEqual(
            iter_rows.call_args.kwargs["start_minute"],
            service.intraday_repository.iso_to_epoch_minute(
                "2024-01-03T00:00:00Z"
            ),
        )

    @patch.object(service.repository, "upsert_daily_prices")
    @patch.object(service.repository, "upsert_daily_price_series")
    @patch.object(service.repository, "delete_daily_prices", return_value=0)
    @patch.object(service.repository, "get_daily_prices", return_value=[])
    @patch(
        "services.backtest.market_calendar.ensure_market_sessions",
        return_value=[{
            "trading_date": "2024-01-02",
            "open_minute_utc": 0,
            "close_minute_utc": 1,
            "is_early_close": False,
        }],
    )
    @patch.object(service.intraday_repository, "iter_minute_bars")
    def test_crypto_derivation_writes_named_us_session_series_only(
        self,
        iter_rows,
        _sessions,
        _legacy_rows,
        _delete_legacy,
        upsert_series,
        upsert_native,
    ) -> None:
        iter_rows.return_value = iter([
            minute_row("2024-01-02T14:30:00Z", 100, 10),
            minute_row("2024-01-02T20:59:00Z", 105, 20),
        ])
        upsert_series.return_value = 1

        result = service.derive_daily_prices_from_minutes("BTC/USD")

        upsert_native.assert_not_called()
        self.assertEqual(upsert_series.call_args.args[:2], (
            "BTC/USD",
            "US_EQUITY_SESSION",
        ))
        self.assertEqual(upsert_series.call_args.args[2][0]["date"], "2024-01-02")
        self.assertEqual(result["updated_rows"], 1)

    @patch.object(service.repository, "get_symbol")
    def test_unsupported_symbol_returns_explicit_intraday_warning(
        self,
        get_symbol,
    ) -> None:
        get_symbol.return_value = {
            "symbol": "SPX",
            "alpaca_supported": False,
            "alpaca_error": "Alpaca 未收录该标的。",
        }

        payload = service.get_chart_bars("SPX", "15m")

        self.assertEqual(payload["data"], [])
        self.assertIn("Alpaca", payload["warning"])


if __name__ == "__main__":
    unittest.main()
