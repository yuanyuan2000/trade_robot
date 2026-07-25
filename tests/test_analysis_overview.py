from __future__ import annotations

import unittest

from services.analysis_overview_service import (
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


class AnalysisOverviewTests(unittest.TestCase):
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
