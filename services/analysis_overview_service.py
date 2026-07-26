from __future__ import annotations

from services.trendline_analysis_service import ANALYSIS_CACHE_VERSION


TIER_LABELS = {
    "long": "长期",
    "medium": "中期",
    "short": "短期",
}
DIRECTION_LABELS = {
    "up": "上涨",
    "down": "下跌",
}
EVENT_PRIORITIES = {
    "ended": 100,
    "formed": 90,
    "stage_formed": 88,
    "challenge_started": 80,
    "challenge_resolved": 70,
    "new_touch": 60,
}


def snapshot_matches_signature(
        snapshot: dict | None,
        signature: dict,
) -> bool:
    return bool(
        snapshot
        and snapshot.get("show_weekend_data")
        == signature.get("show_weekend_data")
        and snapshot.get("payload", {}).get("data_fingerprint")
        == signature.get("data_fingerprint")
        and snapshot.get("payload", {}).get("period")
        == signature.get("period")
        and int(
            snapshot.get("payload", {}).get("requested_window_size")
            or 0
        )
        == int(signature.get("requested_window_size") or 0)
    )


def build_trendline_overview_summary(payload: dict) -> dict:
    trends = payload.get("trends") or []
    latest_index = int(payload.get("data_count") or 0) - 1
    active = [trend for trend in trends if trend.get("active")]
    headline = sorted(active, key=_headline_rank)
    events: list[dict] = []

    for trend in trends:
        event = _latest_trend_event(trend, latest_index)
        if event:
            events.append(event)

    events.sort(
        key=lambda event: (
            event["priority"],
            float(event.get("score") or 0),
        ),
        reverse=True,
    )
    headline_trends = [
        _compact_trend(trend)
        for trend in headline
    ]
    _assign_overview_tier_labels(headline_trends)
    visible_headlines = headline_trends[:2]
    return {
        "period": payload.get("period") or "1D",
        "window_size": int(payload.get("requested_window_size") or 150),
        "data_date": payload.get("latest_data_date"),
        "active_count": len(active),
        "challenging_count": sum(
            1 for trend in active if trend.get("status") == "challenging"
        ),
        "highest_score": max(
            (float(trend.get("score") or 0) for trend in visible_headlines),
            default=0.0,
        ),
        "headline_trends": headline_trends,
        "events": events,
    }


def _headline_rank(trend: dict) -> tuple:
    status_rank = 1 if trend.get("status") == "challenging" else 0
    role_rank = {
        "primary": 2,
        "standalone": 1,
        "stage": 0,
    }.get(trend.get("family_role"), 0)
    return (
        -status_rank,
        -role_rank,
        -float(trend.get("tier_score") or 0),
    )


def _compact_trend(trend: dict) -> dict:
    return {
        "id": trend.get("id"),
        "direction": trend.get("direction"),
        "tier": trend.get("tier"),
        "status": trend.get("status"),
        "score": float(trend.get("tier_score") or 0),
        "latest_line_price": trend.get("projection_end_price"),
        "structure_length": (
            int(trend["end_index"]) - int(trend["start_index"]) + 1
            if (
                trend.get("start_index") is not None
                and trend.get("end_index") is not None
            )
            else 0
        ),
        "display_length": (
            int(trend["projection_end_index"])
            - int(trend["start_index"])
            + 1
            if (
                trend.get("start_index") is not None
                and trend.get("projection_end_index") is not None
            )
            else 0
        ),
        "family_role": trend.get("family_role") or "standalone",
        "touches": int(trend.get("touches") or 0),
        "start_date": trend.get("start_date"),
        "formation_date": trend.get("formation_date"),
        "last_touch_date": trend.get("last_touch_date"),
        "current_close_gap": trend.get("current_close_gap"),
    }


def _assign_overview_tier_labels(trends: list[dict]) -> None:
    visible = trends[:2]
    medium = [trend for trend in visible if trend["tier"] == "medium"]
    if len(medium) != 2:
        return
    medium.sort(
        key=lambda trend: int(trend.get("display_length") or 0),
        reverse=True,
    )
    medium[0]["overview_tier_label"] = "中长期"
    medium[1]["overview_tier_label"] = "中短期"


def _latest_trend_event(trend: dict, latest_index: int) -> dict | None:
    if latest_index < 0:
        return None

    event_type = None
    text = None
    detail = None
    direction = trend.get("direction")
    tier = trend.get("tier")
    direction_label = DIRECTION_LABELS.get(direction, str(direction or ""))
    tier_label = TIER_LABELS.get(tier, str(tier or ""))

    if trend.get("termination_confirmed_index") == latest_index:
        event_type = "ended"
        if trend.get("end_reason") == "acceleration":
            text = f"{direction_label}{tier_label}旧趋势今日结束"
            detail = "价格持续远离 4 ATR，并完成后续 3 根确认"
        else:
            boundary = "支撑" if direction == "up" else "压力"
            text = f"{direction_label}{tier_label}{boundary}今日结束"
            detail = (
                "单根收盘严重突破确认"
                if float(trend.get("current_close_gap") or 0) < -0.80
                else "连续两根收盘突破确认"
            )
    elif trend.get("formation_end_index") == latest_index:
        event_type = (
            "stage_formed"
            if trend.get("family_role") == "stage"
            else "formed"
        )
        qualifier = "阶段线" if event_type == "stage_formed" else "趋势"
        text = f"{direction_label}{tier_label}{qualifier}今日确认"
        detail = f"达到最低确认触点数，共 {int(trend.get('touches') or 0)} 个触点"
    elif trend.get("active"):
        previous_gap = float(trend.get("previous_close_gap") or 0)
        current_gap = float(trend.get("current_close_gap") or 0)
        if previous_gap > 0.50 and current_gap <= 0.50:
            event_type = "challenge_started"
            text = f"{direction_label}{tier_label}趋势进入挑战"
            detail = f"最新收盘距趋势线 {current_gap:.2f} ATR"
        elif previous_gap <= 0.50 and current_gap > 0.50:
            event_type = "challenge_resolved"
            text = f"重回{direction_label}{tier_label}趋势"
            detail = f"最新收盘已回到趋势线正确一侧 {current_gap:.2f} ATR"
        elif latest_index in (trend.get("touch_indices") or []):
            event_type = "new_touch"
            touches = int(trend.get("touches") or 0)
            text = f"{direction_label}{tier_label}新增第 {touches} 个触点"
            detail = "最新 K 线构成新的独立有效触点"

    if not event_type:
        return None
    return {
        "type": event_type,
        "priority": EVENT_PRIORITIES[event_type],
        "direction": direction,
        "tier": tier,
        "score": float(trend.get("tier_score") or 0),
        "text": text,
        "detail": detail,
    }


def merge_analysis_overview(
        market_overview: dict,
        snapshots: dict[str, dict],
) -> dict:
    items = []
    for market_item in market_overview.get("items") or []:
        snapshot = snapshots.get(market_item["symbol"])
        if (
            snapshot
            and bool(snapshot.get("show_weekend_data"))
            != bool(market_item.get("show_weekend_data"))
        ):
            snapshot = None
        analysis = None
        if snapshot:
            analysis = {
                **snapshot["summary"],
                "computed_at": snapshot["computed_at"],
                "algorithm_version": ANALYSIS_CACHE_VERSION,
                "stale": (
                    snapshot.get("latest_data_date")
                    != market_item.get("analysis_latest_date")
                ),
            }
        items.append({**market_item, "analysis": analysis})
    return {**market_overview, "items": items}
