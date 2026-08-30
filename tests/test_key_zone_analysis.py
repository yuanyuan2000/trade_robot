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
        index = np.arange(120)
        close = 104 + 3.5 * np.sin(2 * np.pi * index / 14) + 0.005 * index
        two_sided_range = pd.DataFrame({
            "Open": close,
            "High": close + 0.45,
            "Low": close - 0.45,
            "Close": close,
            "Volume": np.full(120, 1_000_000),
        })
        result = detect_key_zones(two_sided_range)
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

    def test_confirmed_break_changes_role_but_remains_pending(self) -> None:
        frame = horizontal_range()
        close = frame["Close"].to_numpy(copy=True)
        close[-8:] = np.linspace(102, 96, 8)
        frame["Close"] = close
        frame["Open"] = np.r_[close[0], close[:-1]]
        frame["High"] = frame[["Open", "Close"]].max(axis=1) + 0.45
        frame["Low"] = frame[["Open", "Close"]].min(axis=1) - 0.45

        zones = detect_key_zones(
            frame,
            KeyZoneConfig(acceptance_distance_atr=10.0),
        )["zones"]
        support = min(zones, key=lambda zone: abs(zone["center"] - 104.5))
        self.assertEqual(support["current_role"], "resistance")
        self.assertEqual(support["status"], "retesting")
        self.assertTrue(support["active"])
        self.assertIsNotNone(support["break_index"])
        self.assertFalse(support["role_reversal_confirmed"])

    def test_break_changes_role_immediately_and_uses_three_display_states(self) -> None:
        cfg = KeyZoneConfig()
        tests = pd.DataFrame({
            "confirmed_at_index": [0, 2],
            "type": ["high", "high"],
        })

        pending = pd.DataFrame({
            "close": [9.0, 9.0, 9.0, 11.4, 11.4, 13.0],
            "atr": np.ones(6),
        })
        pending_state = service._zone_state(pending, tests, 9.0, 11.0, cfg)
        self.assertEqual(pending_state["current_role"], "support")
        self.assertEqual(pending_state["status"], "retesting")

        challenging = pending.copy()
        challenging.loc[5, "close"] = 11.2
        challenging_state = service._zone_state(
            challenging, tests, 9.0, 11.0, cfg,
        )
        self.assertEqual(challenging_state["current_role"], "support")
        self.assertEqual(challenging_state["status"], "challenging")

        accepted = pending.copy()
        accepted.loc[5, "close"] = 16.0
        accepted_state = service._zone_state(accepted, tests, 9.0, 11.0, cfg)
        self.assertEqual(accepted_state["current_role"], "support")
        self.assertEqual(accepted_state["status"], "active")
        self.assertTrue(accepted_state["role_reversal_confirmed"])

    def test_four_atr_accepts_a_pending_role_reversal(self) -> None:
        frame = pd.DataFrame({
            "close": [9.0, 9.0, 9.0, 11.4, 11.4, 15.0],
            "atr": np.ones(6),
        })
        tests = pd.DataFrame({
            "confirmed_at_index": [0, 2],
            "type": ["high", "high"],
        })

        state = service._zone_state(
            frame, tests, 9.0, 11.0, KeyZoneConfig(),
        )

        self.assertEqual(KeyZoneConfig().acceptance_distance_atr, 4.0)
        self.assertEqual(state["status"], "active")
        self.assertTrue(state["role_reversal_confirmed"])

    def test_confirmation_day_complete_rejection_is_not_challenging(self) -> None:
        frame = pd.DataFrame({
            "close": [8.0, 8.0, 8.0, 8.0, 8.0, 8.0],
            "high": [8.5, 8.5, 8.5, 8.5, 8.5, 9.8],
            "low": [7.5, 7.5, 7.5, 7.5, 7.5, 7.5],
            "atr": np.ones(6),
        })
        tests = pd.DataFrame({
            "confirmed_at_index": [2, 5],
            "type": ["high", "high"],
        })

        state = service._zone_state(
            frame, tests, 10.0, 12.0, KeyZoneConfig(),
        )

        self.assertEqual(state["status"], "active")
        self.assertEqual(state["validation_events"], [])

    def test_new_confirmed_cross_replaces_an_older_pending_retest(self) -> None:
        frame = pd.DataFrame({
            "close": [12.0, 12.0, 12.0, 8.5, 8.5, 11.5, 11.5, 12.0, 13.0],
            "atr": np.ones(9),
        })
        tests = pd.DataFrame({
            "confirmed_at_index": [0, 2, 7],
            "type": ["low", "low", "low"],
        })

        state = service._zone_state(
            frame, tests, 9.0, 11.0, KeyZoneConfig(),
        )

        self.assertEqual(state["current_role"], "support")
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["role_reversal_count"], 2)
        self.assertTrue(state["role_reversal_confirmed"])

    def test_isolated_strict_pivot_can_be_rescued_by_confirmed_shoulder(self) -> None:
        frame = pd.DataFrame({
            "high": np.full(20, 95.0),
            "low": np.full(20, 94.0),
            "close": np.full(20, 94.5),
            "atr": np.full(20, 2.0),
        })
        frame.loc[5, "high"] = 100.0
        frame.loc[9, "high"] = 100.0
        frame.loc[12, "high"] = 99.6
        anchor = pd.Series({
            "pivot_index": 5,
            "confirmed_at_index": 8,
            "type": "high",
            "price": 100.0,
            "atr": 2.0,
        })
        strict = pd.DataFrame([anchor])

        shoulders = service._shoulder_tests_for_anchor(
            frame, anchor, strict, KeyZoneConfig(),
        )
        unconfirmed = service._shoulder_tests_for_anchor(
            frame.iloc[:15].copy(), anchor, strict, KeyZoneConfig(),
        )

        self.assertEqual(shoulders["pivot_index"].astype(int).tolist(), [12])
        self.assertEqual(shoulders.iloc[0]["evidence_source"], "shoulder")
        self.assertTrue(unconfirmed.empty)

    def test_zone_validation_groups_nearby_rejections_without_changing_geometry(
            self,
    ) -> None:
        frame = pd.DataFrame({
            "high": np.full(18, 95.0),
            "low": np.full(18, 94.0),
            "close": np.full(18, 94.5),
            "atr": np.full(18, 2.0),
        })
        frame.loc[5, ["high", "close"]] = [99.5, 97.0]
        frame.loc[8, ["high", "close"]] = [100.5, 96.0]
        frame.loc[15, ["high", "close"]] = [99.8, 97.5]

        events = service._zone_validation_events(
            frame, 0, "resistance", 100.0, 102.0, KeyZoneConfig(),
        )

        self.assertEqual([event["index"] for event in events], [8, 15])
        self.assertGreater(events[0]["rejection_atr"], 2.0)

    def test_third_last_bar_uses_normal_pivot_rules_with_two_right_bars(
            self,
    ) -> None:
        frame = pd.DataFrame({
            "high": np.full(20, 104.0),
            "low": np.full(20, 103.0),
            "close": np.full(20, 103.5),
            "atr": np.full(20, 2.0),
        })
        frame.loc[17, ["high", "low", "close"]] = [103.0, 100.2, 102.5]
        frame.loc[18, ["high", "low", "close"]] = [102.8, 102.0, 102.4]
        frame.loc[19, ["high", "low", "close"]] = [103.0, 102.5, 102.8]
        strict = pd.DataFrame(columns=["type", "pivot_index"])

        confirmed = service._provisional_edge_pivots(
            frame, strict, KeyZoneConfig(),
        )
        one_right_bar = service._provisional_edge_pivots(
            frame.iloc[:19].copy(), strict, KeyZoneConfig(),
        )

        self.assertEqual(confirmed["pivot_index"].astype(int).tolist(), [17])
        self.assertEqual(confirmed.iloc[0]["type"], "low")
        self.assertEqual(
            confirmed.iloc[0]["evidence_source"],
            "provisional_edge",
        )
        self.assertLess(confirmed.iloc[0]["rejection_atr"], 2.0)
        self.assertTrue(one_right_bar.empty)

    def test_validation_rejection_confirms_pending_new_role(self) -> None:
        frame = pd.DataFrame({
            "close": [9.0, 9.0, 9.0, 11.4, 11.4, 13.0, 13.0],
            "high": [9.5, 9.5, 9.5, 11.8, 11.8, 13.2, 13.2],
            "low": [8.8, 8.8, 8.8, 11.1, 11.1, 10.8, 12.5],
            "atr": np.ones(7),
        })
        tests = pd.DataFrame({
            "confirmed_at_index": [0, 2],
            "type": ["high", "high"],
        })

        state = service._zone_state(
            frame, tests, 9.0, 11.0, KeyZoneConfig(),
        )

        self.assertEqual(state["current_role"], "support")
        self.assertEqual(state["status"], "active")
        self.assertTrue(state["role_reversal_confirmed"])
        self.assertEqual(
            [event["index"] for event in state["validation_events"]],
            [5],
        )

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
