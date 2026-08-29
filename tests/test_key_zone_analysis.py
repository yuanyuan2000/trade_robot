from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import app as app_module
from services import key_zone_analysis_service as service
from services.key_zone_analysis_service import (
    KeyZoneConfig,
    analyze_symbol_key_zones,
    detect_confirmed_pivots,
    detect_key_zones,
)


def horizontal_range(n: int = 120, scale: float = 1.0) -> pd.DataFrame:
    index = np.arange(n)
    close = 104 + 3.5 * np.abs(np.sin(np.pi * index / 14)) + 0.015 * index
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.45
    low = np.minimum(open_, close) - 0.45
    return pd.DataFrame(
        {
            "Open": open_ * scale,
            "High": high * scale,
            "Low": low * scale,
            "Close": close * scale,
            "Volume": np.full(n, 1_000_000),
        }
    )


def prepared_payload(n: int = 120, window_start: int = 20) -> dict:
    frame = horizontal_range(n)
    candles = []
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    for index, row in frame.iterrows():
        candles.append(
            {
                "date": dates[index].strftime("%Y-%m-%d"),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
        )
    window = candles[window_start:]
    return {
        "market_payload": {
            "ok": True,
            "symbol": "TEST",
            "canonical_symbol": "TEST",
            "source": "database",
        },
        "period": "1D",
        "requested_window_size": len(window),
        "show_weekend_data": True,
        "candles": candles,
        "window_start": window_start,
        "window": window,
        "data_fingerprint": "test-fingerprint",
    }


class KeyZoneCoreTests(unittest.TestCase):
    def test_default_sensitivity_uses_local_confirmed_swings(self) -> None:
        cfg = KeyZoneConfig()

        self.assertEqual(cfg.pivot_left_bars, 5)
        self.assertEqual(cfg.pivot_right_bars, 3)
        self.assertEqual(cfg.dbscan_eps_atr, 0.75)

    def test_pivots_have_fixed_confirmation_delay(self) -> None:
        cfg = KeyZoneConfig(pivot_right_bars=5)
        _, pivots = detect_confirmed_pivots(horizontal_range(), cfg)

        self.assertFalse(pivots.empty)
        self.assertTrue(
            np.array_equal(
                pivots["confirmed_at_index"].to_numpy(),
                pivots["pivot_index"].to_numpy() + 5,
            )
        )
        self.assertLessEqual(int(pivots["pivot_index"].max()), 114)

    def test_appending_future_bars_does_not_change_confirmed_pivot_structure(
            self,
    ) -> None:
        prefix = horizontal_range(90)
        suffix = horizontal_range(120)
        _, before = detect_confirmed_pivots(prefix)
        _, after = detect_confirmed_pivots(suffix)
        after = after[
            after["confirmed_at_index"] < len(prefix)
        ].reset_index(drop=True)
        structural_columns = [
            "pivot_index",
            "confirmed_at_index",
            "type",
            "price",
            "atr",
            "prominence_atr",
            "rejection_atr",
        ]

        pd.testing.assert_frame_equal(
            before[structural_columns].reset_index(drop=True),
            after[structural_columns],
            check_exact=False,
            rtol=0,
            atol=1e-12,
        )

    def test_repeated_levels_form_support_and_resistance_zones(self) -> None:
        result = detect_key_zones(horizontal_range())
        active = [zone for zone in result["zones"] if zone["active"]]

        self.assertGreaterEqual(len(active), 2)
        self.assertEqual(
            {zone["current_role"] for zone in active},
            {"support", "resistance"},
        )
        self.assertTrue(all(0 <= zone["score"] <= 100 for zone in active))
        self.assertTrue(
            all(
                zone["formation_index"]
                == zone["confirmed_indices"][1]
                for zone in active
            )
        )
        self.assertTrue(
            all(
                zone["display_start_index"] == zone["start_index"]
                and zone["integrity_start_index"] == zone["start_index"]
                and zone["display_end_index"]
                == zone["latest_confirmed_index"]
                and min(zone["touch_indices"])
                >= zone["display_start_index"]
                and max(zone["confirmed_indices"])
                <= zone["display_end_index"]
                for zone in active
            )
        )

    def test_price_scaling_preserves_zone_geometry_in_atr_units_and_score(
            self,
    ) -> None:
        base = detect_key_zones(horizontal_range())["zones"]
        scaled = detect_key_zones(horizontal_range(scale=10))["zones"]

        self.assertEqual(len(base), len(scaled))
        for original, multiplied in zip(base, scaled):
            self.assertAlmostEqual(multiplied["center"], original["center"] * 10)
            self.assertAlmostEqual(multiplied["zone_low"], original["zone_low"] * 10)
            self.assertAlmostEqual(multiplied["zone_high"], original["zone_high"] * 10)
            self.assertAlmostEqual(multiplied["score"], original["score"])
            self.assertEqual(multiplied["status"], original["status"])

    def test_integrity_reduces_the_whole_score_for_repeatedly_crossed_zones(
            self,
    ) -> None:
        frame = pd.DataFrame(index=range(150))
        tests = pd.DataFrame({
            "pivot_index": [20, 120],
            "confirmed_at_index": [25, 125],
            "prominence_atr": [2.0, 2.0],
            "rejection_atr": [3.0, 3.0],
        })
        intact, intact_components = service._score_zone(
            frame, tests, 1.0, KeyZoneConfig(),
        )
        crossed, crossed_components = service._score_zone(
            frame, tests, 0.2, KeyZoneConfig(),
        )

        self.assertEqual(intact_components["integrity_multiplier"], 1.0)
        self.assertAlmostEqual(
            crossed_components["integrity_multiplier"], 0.6,
        )
        self.assertLess(crossed, intact * 0.7)

    def test_confirmed_break_requires_retest_before_confirming_role_flip(self) -> None:
        frame = horizontal_range()
        close = frame["Close"].to_numpy(copy=True)
        close[-8:] = np.linspace(102, 96, 8)
        frame["Close"] = close
        frame["Open"] = np.r_[close[0], close[:-1]]
        frame["High"] = frame[["Open", "Close"]].max(axis=1) + 0.45
        frame["Low"] = frame[["Open", "Close"]].min(axis=1) - 0.45

        zones = detect_key_zones(frame)["zones"]
        support = min(zones, key=lambda zone: abs(zone["center"] - 104.5))
        self.assertEqual(support["current_role"], "resistance")
        self.assertEqual(support["status"], "retesting")
        self.assertTrue(support["active"])
        self.assertIsNotNone(support["break_index"])
        self.assertFalse(support["role_reversal_confirmed"])

    def test_invalid_ohlc_is_rejected(self) -> None:
        frame = horizontal_range(40)
        frame.loc[20, "High"] = frame.loc[20, "Low"] - 1
        with self.assertRaisesRegex(ValueError, "invalid high/low"):
            detect_key_zones(frame)


class KeyZoneServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        service.clear_key_zone_analysis_cache()

    @patch.object(service, "_prepare_trendline_analysis")
    def test_symbol_response_is_json_serializable_and_uses_global_indices(
            self,
            prepare,
    ) -> None:
        prepared = prepared_payload()
        prepare.return_value = prepared

        payload = analyze_symbol_key_zones("TEST", limit=100)

        json.dumps(payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["algorithm"], "key_zones")
        self.assertEqual(payload["window_start_index"], 20)
        self.assertGreaterEqual(len(payload["zones"]), 1)
        self.assertTrue(
            all(zone["start_index"] >= 20 for zone in payload["zones"])
        )
        first = payload["zones"][0]
        self.assertIsNotNone(first["formation_date"])
        self.assertIsNotNone(first["latest_confirmed_date"])
        self.assertEqual(first["display_start_index"], first["start_index"])
        self.assertEqual(first["integrity_start_index"], first["start_index"])
        self.assertEqual(
            first["display_end_index"],
            first["latest_confirmed_index"],
        )


class KeyZoneRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()

    @patch.object(app_module.repository, "save_key_zone_analysis_snapshot")
    @patch.object(app_module, "analyze_symbol_key_zones")
    def test_key_zone_route_forwards_analysis_settings(
            self,
            analyze,
            save_snapshot,
    ) -> None:
        payload = {"ok": True, "zones": []}
        analyze.return_value = payload

        response = self.client.get(
            "/api/analysis/key-zones?symbol=SPY&period=1W&limit=150"
            "&show_non_us_market_days=0&adjustment=split"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "zones": []})
        analyze.assert_called_once_with(
            "SPY",
            period="1W",
            limit=150,
            show_weekend_data="0",
            adjustment="split",
        )
        save_snapshot.assert_called_once_with(
            "SPY",
            payload,
            service.KEY_ZONE_ALGORITHM_VERSION,
        )

    @patch.object(app_module.repository, "get_latest_key_zone_analysis_snapshot")
    def test_key_zone_snapshot_route_uses_exact_chart_settings(
            self,
            get_snapshot,
    ) -> None:
        get_snapshot.return_value = {"payload": {"zones": []}}

        response = self.client.get(
            "/api/analysis/key-zone-snapshot?symbol=SPY&period=1W&limit=150"
            "&show_non_us_market_days=0&adjustment=split"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["snapshot"],
            {"payload": {"zones": []}},
        )
        get_snapshot.assert_called_once_with(
            "SPY",
            service.KEY_ZONE_ALGORITHM_VERSION,
            period="1W",
            window_size=150,
            show_weekend_data=False,
            adjustment="split",
        )

    def test_index_exposes_key_zone_algorithm_option(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('option value="key_zones"', html)
        chart_path = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "chart.js"
        )
        chart_script = chart_path.read_text(encoding="utf-8")
        self.assertIn("distance >= 5", chart_script)
        self.assertIn("project_center_to_current", chart_script)
        app_script = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Number(right.score || 0) - Number(left.score || 0)", app_script)

    @patch.object(app_module, "analyze_symbol_key_zones")
    def test_key_zone_route_returns_invalid_input(self, analyze) -> None:
        analyze.side_effect = ValueError("Unsupported analysis period")

        response = self.client.get(
            "/api/analysis/key-zones?symbol=SPY&period=1m"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
