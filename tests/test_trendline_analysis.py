from __future__ import annotations

from dataclasses import replace
import unittest

import numpy as np
import pandas as pd

from services.trendline_analysis_service import (
    TieredTrend,
    _body_integrity_batch,
    _body_metrics,
    _cached_analysis_result,
    _is_display_fresh,
    _lines_are_duplicates,
    _store_analysis_result,
    _tier_boundaries,
    _window_fingerprint,
    clear_trendline_analysis_cache,
    fit_interval,
    medium_trend_score,
    search_trend_hierarchy,
    serialize_trend,
    true_range,
)


def make_ohlc(close: np.ndarray, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.08, len(close))
    body_high = np.maximum(open_, close)
    body_low = np.minimum(open_, close)
    high = body_high + rng.uniform(0.22, 0.38, len(close))
    low = body_low - rng.uniform(0.22, 0.38, len(close))
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": np.full(len(close), 1_000_000),
        }
    )


def distributed_support(n: int = 90) -> pd.DataFrame:
    t = np.arange(n)
    baseline = 100 + 0.14 * t
    pullback_cycle = 3.2 * np.abs(np.sin(np.pi * t / 12))
    return make_ohlc(baseline + 0.30 + pullback_cycle, seed=17)


def endpoint_bridge(n: int = 90) -> pd.DataFrame:
    t = np.arange(n)
    baseline = 100 + 0.14 * t
    suspended_middle = 6.0 * np.sin(np.pi * t / (n - 1)) ** 2
    return make_ohlc(baseline + 0.30 + suspended_middle, seed=23)


class TrendlineAnalysisTests(unittest.TestCase):
    def test_150_bar_tiers_use_15_and_50_bar_boundaries(self) -> None:
        self.assertEqual(_tier_boundaries(150), (15, 50))

    def test_repeated_contacts_beat_endpoint_only_bridge(self) -> None:
        repeated = fit_interval(distributed_support(), 0, 89, "up")
        bridge = fit_interval(endpoint_bridge(), 0, 89, "up")

        self.assertIsNotNone(repeated)
        self.assertIsNotNone(bridge)
        assert repeated is not None and bridge is not None
        self.assertGreaterEqual(repeated.touches, 4)
        self.assertGreater(repeated.touch_score, bridge.touch_score)
        self.assertGreater(repeated.touch_distribution, bridge.touch_distribution)
        self.assertLess(repeated.max_touch_gap, 0.80)
        self.assertGreaterEqual(bridge.max_touch_gap, 0.80)

    def test_medium_score_rewards_three_or_more_contacts(self) -> None:
        trend = fit_interval(distributed_support(42), 0, 41, "up")
        self.assertIsNotNone(trend)
        assert trend is not None
        two_touch_version = replace(
            trend,
            touches=2,
            touch_score=trend.touch_score * 0.55,
            touch_distribution=0.12,
            max_touch_gap=1.0,
        )
        self.assertGreater(
            medium_trend_score(trend),
            medium_trend_score(two_touch_version) + 5.0,
        )

    def test_consecutive_body_crossings_receive_large_penalty(self) -> None:
        single_cross = np.ones(20)
        single_cross[8] = -0.45
        repeated_cross = np.ones(20)
        repeated_cross[8:11] = -0.45

        single = _body_metrics(single_cross)
        repeated = _body_metrics(repeated_cross)
        self.assertEqual(single.max_breach_run, 1)
        self.assertEqual(repeated.max_breach_run, 3)
        self.assertLess(repeated.integrity, single.integrity * 0.35)

    def test_batch_body_integrity_matches_scalar_calculation(self) -> None:
        rng = np.random.default_rng(41)
        gaps = rng.normal(0.15, 0.45, size=(12, 37))
        expected = np.asarray([
            _body_metrics(row).integrity
            for row in gaps
        ])
        np.testing.assert_allclose(
            _body_integrity_batch(gaps),
            expected,
            rtol=0,
            atol=1e-15,
        )

    def test_analysis_cache_uses_fingerprint_and_defensive_copies(self) -> None:
        clear_trendline_analysis_cache()
        candles = [
            {
                "date": "2026-07-22",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
            }
        ]
        fingerprint = _window_fingerprint(candles)
        key = ("test", fingerprint)
        _store_analysis_result(key, [{"tier_score": 75.0}])
        first = _cached_analysis_result(key)
        assert first is not None
        first[0]["tier_score"] = 1.0
        second = _cached_analysis_result(key)
        assert second is not None
        self.assertEqual(second[0]["tier_score"], 75.0)

        candles[0]["close"] = 101.5
        self.assertNotEqual(_window_fingerprint(candles), fingerprint)

    def test_duplicate_geometry_is_cross_window_not_just_date_overlap(self) -> None:
        df = distributed_support()
        first = fit_interval(df, 0, 82, "up")
        second = fit_interval(df, 2, 84, "up")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertTrue(_lines_are_duplicates(df, first, second))

        atr_shift = 1.2 * float(np.median(true_range(df)))
        distinct = replace(second, intercept=second.intercept + atr_shift)
        self.assertFalse(_lines_are_duplicates(df, first, distinct))

    def test_endpoint_bridge_is_not_returned_as_one_long_structure(self) -> None:
        df = endpoint_bridge(90)
        hierarchy = search_trend_hierarchy(df)
        covering = [
            item
            for item in hierarchy["long"]
            if item.trend.length >= 72
        ]
        self.assertEqual(covering, [])

    def test_serialization_draws_only_between_confirmed_contacts(self) -> None:
        trend = fit_interval(distributed_support(), 0, 89, "up")
        self.assertIsNotNone(trend)
        assert trend is not None
        item = TieredTrend(
            tier="long",
            trend=trend,
            active=False,
            tier_score=trend.score,
        )
        payload = serialize_trend(item, window_start=100, latest_index=249)
        self.assertEqual(payload["start_index"], 100 + trend.first_touch)
        self.assertEqual(payload["end_index"], 100 + trend.last_touch)
        self.assertEqual(payload["projection_end_index"], payload["end_index"])
        self.assertEqual(payload["fit_start_index"], 100 + trend.start)
        self.assertEqual(payload["fit_end_index"], 100 + trend.end)

        item.active = True
        active_payload = serialize_trend(
            item,
            window_start=100,
            latest_index=249,
        )
        self.assertEqual(active_payload["end_index"], 100 + trend.last_touch)
        self.assertEqual(active_payload["projection_end_index"], 249)

    def test_old_lines_need_high_scores_to_remain_visible(self) -> None:
        self.assertTrue(_is_display_fresh(77.0, 60))
        self.assertFalse(_is_display_fresh(77.0, 90))
        self.assertTrue(_is_display_fresh(79.0, 90))
        self.assertFalse(_is_display_fresh(79.0, 120))
        self.assertTrue(_is_display_fresh(81.0, 120))


if __name__ == "__main__":
    unittest.main()
