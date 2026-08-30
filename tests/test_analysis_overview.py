from __future__ import annotations

import unittest

from services.analysis_overview_service import (
    build_key_zone_overview_summary,
    build_trendline_overview_summary,
    merge_analysis_overview,
    snapshot_matches_signature,
)


def trend(**overrides) -> dict:
    value = {
        "id": "M-up-10-30",
        "direction": "up",
        "tier": "medium",
        "status": "trending",
        "active": True,
        "tier_score": 78.0,
        "family_role": "primary",
        "touches": 3,
        "touch_indices": [10, 20, 30],
        "start_index": 10,
        "end_index": 30,
        "formation_end_index": 20,
        "projection_end_index": 30,
        "projection_end_price": 123.45,
        "termination_confirmed_index": None,
        "end_reason": None,
        "previous_close_gap": 0.8,
        "current_close_gap": 0.9,
        "start_date": "2026-01-10",
        "formation_date": "2026-01-20",
        "last_touch_date": "2026-01-30",
    }
    value.update(overrides)
    return value


def payload(*trends: dict, data_count: int = 31) -> dict:
    return {
        "period": "1D",
        "requested_window_size": 150,
        "data_count": data_count,
        "latest_data_date": "2026-01-30",
        "trends": list(trends),
    }


def zone(**overrides) -> dict:
    value = {
        "id": "resistance-100",
        "active": True,
        "current_role": "resistance",
        "status": "challenging",
        "center": 100.0,
        "zone_low": 99.0,
        "zone_high": 101.0,
        "score": 80.0,
        "distance_from_current_atr": 0.4,
        "break_date": None,
        "latest_test_date": "2026-01-30",
        "latest_validation_date": "2026-01-30",
    }
    value.update(overrides)
    return value


def key_zone_payload(*zones: dict) -> dict:
    return {
        "period": "1D",
        "requested_window_size": 150,
        "latest_data_date": "2026-01-30",
        "algorithm_version": "key-zone-test",
        "zones": list(zones),
    }


class AnalysisOverviewTests(unittest.TestCase):
    def test_key_zone_summary_only_keeps_critical_active_states(self) -> None:
        summary = build_key_zone_overview_summary(key_zone_payload(
            zone(id="testing"),
            zone(id="valid", status="valid"),
            zone(id="inactive", active=False),
            zone(id="broken", status="broken"),
        ))

        self.assertEqual(summary["critical_count"], 1)
        self.assertEqual(
            [item["id"] for item in summary["headline_zones"]],
            ["testing"],
        )

    def test_key_zone_summary_only_displays_zones_within_one_point_five_atr(
            self,
    ) -> None:
        summary = build_key_zone_overview_summary(key_zone_payload(
            zone(id="boundary", distance_from_current_atr=1.5),
            zone(
                id="outside",
                current_role="support",
                status="retesting",
                distance_from_current_atr=1.5001,
            ),
        ))

        self.assertEqual(summary["critical_count"], 1)
        self.assertEqual(
            [item["id"] for item in summary["headline_zones"]],
            ["boundary"],
        )

    def test_key_zone_summary_picks_nearest_zone_for_each_role(self) -> None:
        summary = build_key_zone_overview_summary(key_zone_payload(
            zone(id="far-resistance", distance_from_current_atr=1.2),
            zone(id="near-resistance", distance_from_current_atr=0.3),
            zone(
                id="near-support",
                current_role="support",
                status="retesting",
                distance_from_current_atr=0.2,
            ),
            zone(
                id="far-support",
                current_role="support",
                status="retesting",
                distance_from_current_atr=0.8,
            ),
        ))

        self.assertEqual(summary["critical_count"], 4)
        self.assertEqual(
            {item["id"] for item in summary["headline_zones"]},
            {"near-resistance", "near-support"},
        )

    def test_key_zone_summary_prioritizes_testing_in_display_order(self) -> None:
        summary = build_key_zone_overview_summary(key_zone_payload(
            zone(
                id="support-retest",
                current_role="support",
                status="retesting",
                distance_from_current_atr=0.1,
            ),
            zone(id="resistance-testing", distance_from_current_atr=0.6),
        ))

        self.assertEqual(
            [item["id"] for item in summary["headline_zones"]],
            ["resistance-testing", "support-retest"],
        )

    def test_merge_adds_key_zone_summary_and_stale_state(self) -> None:
        market = {
            "items": [{
                "symbol": "SKYY",
                "analysis_latest_date": "2026-01-31",
                "show_weekend_data": False,
            }],
        }
        key_snapshots = {
            "SKYY": {
                "payload": key_zone_payload(zone()),
                "computed_at": "2026-01-30T12:00:00+00:00",
                "latest_data_date": "2026-01-30",
                "show_weekend_data": False,
            },
        }

        merged = merge_analysis_overview(market, {}, key_snapshots)

        key_zones = merged["items"][0]["key_zones"]
        self.assertTrue(key_zones["stale"])
        self.assertEqual(key_zones["headline_zones"][0]["id"], "resistance-100")

    def test_no_active_trend_has_zero_sort_score(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(active=False, status="broken"),
        ))

        self.assertEqual(summary["headline_trends"], [])
        self.assertEqual(summary["highest_score"], 0.0)

    def test_formation_takes_priority_over_latest_touch(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(
                formation_end_index=30,
                touch_indices=[10, 20, 30],
            ),
        ))
        self.assertEqual(summary["events"][0]["type"], "formed")
        self.assertNotIn(
            "new_touch",
            [event["type"] for event in summary["events"]],
        )

    def test_challenge_started_and_resolved_are_inferred_from_two_bars(self) -> None:
        started = build_trendline_overview_summary(payload(
            trend(previous_close_gap=0.8, current_close_gap=0.3),
        ))
        resolved = build_trendline_overview_summary(payload(
            trend(
                status="trending",
                previous_close_gap=0.2,
                current_close_gap=0.9,
            ),
        ))
        self.assertEqual(started["events"][0]["type"], "challenge_started")
        self.assertEqual(resolved["events"][0]["type"], "challenge_resolved")
        self.assertIn("重回上涨", resolved["events"][0]["text"])

    def test_termination_uses_confirmation_day(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(
                active=False,
                status="broken",
                end_reason="acceleration",
                termination_confirmed_index=30,
                current_close_gap=5.0,
            ),
        ))
        event = summary["events"][0]
        self.assertEqual(event["type"], "ended")
        self.assertIn("3 根确认", event["detail"])
        self.assertEqual(summary["active_count"], 0)

    def test_headlines_prioritize_challenge_then_primary_then_score(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(id="stage", family_role="stage", tier_score=90.0),
            trend(id="primary", family_role="primary", tier_score=75.0),
            trend(
                id="challenge",
                family_role="standalone",
                status="challenging",
                tier_score=70.0,
                current_close_gap=0.2,
            ),
        ))
        self.assertEqual(
            [item["id"] for item in summary["headline_trends"]],
            ["challenge", "primary", "stage"],
        )
        self.assertEqual(summary["highest_score"], 75.0)

    def test_latest_touch_is_reported_after_formation(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(touches=4, touch_indices=[5, 12, 20, 30]),
        ))
        event = summary["events"][0]
        self.assertEqual(event["type"], "new_touch")
        self.assertIn("第 4 个触点", event["text"])

    def test_stale_check_uses_the_latest_candle_in_analysis_settings(self) -> None:
        market = {
            "items": [{
                "symbol": "XAU/USD",
                "latest_date": "2026-07-25",
                "analysis_latest_date": "2026-07-24",
            }],
        }
        snapshots = {
            "XAU/USD": {
                "summary": {"active_count": 1},
                "computed_at": "2026-07-25T12:00:00+00:00",
                "latest_data_date": "2026-07-24",
            },
        }
        merged = merge_analysis_overview(market, snapshots)
        self.assertFalse(merged["items"][0]["analysis"]["stale"])

    def test_snapshot_reuse_requires_matching_analysis_inputs(self) -> None:
        signature = {
            "period": "1D",
            "requested_window_size": 150,
            "show_weekend_data": False,
            "data_fingerprint": "same-data",
        }
        snapshot = {
            "show_weekend_data": False,
            "payload": {
                "period": "1D",
                "requested_window_size": 150,
                "data_fingerprint": "same-data",
            },
        }
        self.assertTrue(snapshot_matches_signature(snapshot, signature))
        self.assertFalse(snapshot_matches_signature(
            snapshot,
            {**signature, "data_fingerprint": "changed"},
        ))
        self.assertFalse(snapshot_matches_signature(
            snapshot,
            {**signature, "show_weekend_data": True},
        ))

    def test_headline_contains_latest_line_price_and_structure_length(self) -> None:
        summary = build_trendline_overview_summary(payload(trend()))
        headline = summary["headline_trends"][0]
        self.assertEqual(headline["latest_line_price"], 123.45)
        self.assertEqual(headline["structure_length"], 21)
        self.assertEqual(headline["display_length"], 21)

    def test_two_visible_medium_lines_use_full_display_span(self) -> None:
        summary = build_trendline_overview_summary(payload(
            trend(
                id="shorter",
                direction="down",
                status="challenging",
                start_index=20,
                end_index=30,
                projection_end_index=30,
            ),
            trend(
                id="longer",
                start_index=0,
                end_index=10,
                projection_end_index=30,
            ),
        ))
        labels = {
            item["id"]: item.get("overview_tier_label")
            for item in summary["headline_trends"]
        }
        self.assertEqual(labels["longer"], "中长期")
        self.assertEqual(labels["shorter"], "中短期")


if __name__ == "__main__":
    unittest.main()
