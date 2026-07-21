"""Straight support/resistance trend-line detector for OHLC candles.

The detector is deliberately written as a transparent research prototype:
every score component is returned, so thresholds can be calibrated on real
market data rather than treated as immutable constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from services.market_data_service import get_market_data


Direction = Literal["up", "down"]
Tier = Literal["long", "medium", "short"]
SUPPORTED_PERIODS = {"1D", "3D", "1W", "1M"}


@dataclass
class TrendResult:
    start: int
    end: int
    direction: Direction
    slope: float
    intercept: float
    score: float
    integrity: float
    proximity: float
    touches: int
    touch_score: float
    rejection: float
    event_span: float
    efficiency: float
    slope_strength: float
    drift_t: float

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def y(self, absolute_index: np.ndarray | float) -> np.ndarray:
        return self.intercept + self.slope * (np.asarray(absolute_index) - self.start)


@dataclass
class TieredTrend:
    tier: Tier
    trend: TrendResult
    active: bool
    parent_id: str | None = None
    status: str = "historical"
    age: int = 0
    current_gap: float = 0.0
    tier_score: float = 0.0

    @property
    def id(self) -> str:
        r = self.trend
        return f"{self.tier[0].upper()}-{r.direction}-{r.start + 1}-{r.end + 1}"


def true_range(df: pd.DataFrame) -> np.ndarray:
    cached = df.attrs.get("_trendline_atr")
    if cached is not None and len(cached) == len(df):
        return cached
    prev = df["Close"].shift(1)
    tr = np.maximum(df["High"] - df["Low"],
                    np.maximum((df["High"] - prev).abs(),
                               (df["Low"] - prev).abs()))
    # A short robust smoother is preferable to one global volatility number.
    value = tr.rolling(14, min_periods=2).median().bfill().to_numpy()
    df.attrs["_trendline_atr"] = value
    return value


def anchor_points(df: pd.DataFrame, direction: Direction,
                  body_weight: float = 0.75) -> np.ndarray:
    """Entity edge dominates; half-wick midpoint contributes the remainder."""
    o, c = df["Open"].to_numpy(), df["Close"].to_numpy()
    if direction == "up":
        edge = np.minimum(o, c)
        wick_mid = (df["Low"].to_numpy() + edge) / 2
    else:
        edge = np.maximum(o, c)
        wick_mid = (df["High"].to_numpy() + edge) / 2
    return body_weight * edge + (1 - body_weight) * wick_mid


def _event_metrics(gap: np.ndarray) -> tuple[int, float, float, float, float]:
    """Find scale-aware support/resistance tests and subsequent rejections.

    gap is positive on the valid side of either support or resistance, in ATR.
    A small negative gap is therefore a tolerated false break for both cases.

    Crucially, closeness is measured at structural pullback/rebound pivots, not
    on every candle. Peaks are allowed to be far from a support line and troughs
    are allowed to be far from a resistance line.
    """
    n = len(gap)
    smooth = (pd.Series(gap).rolling(5, center=True, min_periods=1)
              .mean().to_numpy())
    min_distance = max(3, n // 15)
    pivots, properties = find_peaks(-smooth, distance=min_distance,
                                    prominence=0.15)
    prominences = properties.get("prominences", np.zeros(len(pivots)))

    # A valid structural test is within 0.75 ATR of the envelope. This is wider
    # than the old 0.40 ATR band because a line may run just below several lows.
    keep = smooth[pivots] <= 0.75
    candidates = pivots[keep].tolist()
    kept_prominence = prominences[keep].tolist()
    horizon = min(24, max(7, n // 6))

    # scipy's peak finder intentionally excludes endpoints. For live trend
    # detection, however, the newest pullback approaching the line is valuable
    # evidence even before its rejection is fully confirmed.
    edge_width = max(4, n // 15)
    edge_start = max(0, n - edge_width)
    edge_i = edge_start + int(np.argmin(smooth[edge_start:]))
    if smooth[edge_i] <= 0.75 and all(abs(edge_i - j) >= min_distance
                                      for j in candidates):
        left_high = np.max(smooth[max(0, edge_i - horizon):edge_i + 1])
        candidates.append(edge_i)
        kept_prominence.append(max(0.0, float(left_high - smooth[edge_i])))

    if candidates:
        ordered = sorted(zip(candidates, kept_prominence))
        candidates = [x[0] for x in ordered]
        kept_prominence = [x[1] for x in ordered]

    evaluated, qualities = 0, []
    for i, prominence in zip(candidates, kept_prominence):
        future = smooth[i + 1:min(n, i + horizon + 1)]
        if len(future) < 2:
            continue
        evaluated += 1
        rebound = np.max(future) - smooth[i]
        stayed_intact = np.min(future) > -0.85
        q_forward = np.clip(rebound / 1.50, 0, 1)
        q_prominence = np.clip(prominence / 1.20, 0, 1)
        q = (0.70 * q_forward + 0.30 * q_prominence)
        q *= 1.0 if stayed_intact else 0.20
        # A shallow penetration followed by rejection is especially strong.
        if -0.45 <= smooth[i] < 0 and stayed_intact:
            q = min(1.0, q + 0.12)
        qualities.append(q)

    required = 2 if n < 20 else 3
    touch_score = min(1.0, len(candidates) / required)
    event_span = 0.0
    if len(candidates) >= 2:
        event_span = (candidates[-1] - candidates[0]) / max(1, n - 1)
        touch_score *= 0.60 + 0.40 * min(1.0, event_span / 0.60)
    rejection = float(np.mean(qualities)) if evaluated else 0.0

    if candidates:
        # Distance only at pullback pivots defines lower/upper-envelope fit.
        pivot_distance = np.median(np.abs(smooth[candidates]))
        envelope_proximity = float(np.exp(-pivot_distance / 0.65))
    else:
        # No challenges means weak evidence even if the line is geometrically
        # below/above all candles.
        envelope_proximity = 0.0
    return (len(candidates), touch_score, rejection,
            envelope_proximity, event_span)


def _score_line(df: pd.DataFrame, start: int, end: int, direction: Direction,
                slope: float, intercept_local: float) -> TrendResult:
    seg = df.iloc[start:end + 1]
    n = len(seg)
    x = np.arange(n)
    d = 1 if direction == "up" else -1
    atr = np.maximum(true_range(df)[start:end + 1], 1e-9)
    ref = anchor_points(seg, direction)
    line = intercept_local + slope * x
    gap = d * (ref - line) / atr

    tolerated = 0.22
    coverage = np.mean(gap >= -tolerated)
    soft_breach = np.mean(np.clip(-gap - tolerated, 0, None))
    hard_breach = np.mean(gap < -0.80)
    integrity = np.clip((coverage - 0.72) / 0.27, 0, 1)
    integrity *= np.exp(-2.5 * soft_breach - 3.0 * hard_breach)

    touches, touch_score, rejection, proximity, event_span = _event_metrics(gap)

    close = seg["Close"].to_numpy()
    signed_net = d * (close[-1] - close[0])
    path = np.sum(np.abs(np.diff(close))) + 1e-9
    efficiency = float(np.clip(signed_net / path, 0, 1))
    move_atr = d * slope * (n - 1) / (np.median(atr) + 1e-9)
    slope_strength = float(np.clip(move_atr / 3.0, 0, 1))

    signed_returns = d * np.diff(close)
    drift_t = float(np.mean(signed_returns) /
                    (np.std(signed_returns, ddof=1) + 1e-9) * np.sqrt(n - 1))
    # A geometric boundary without statistically directional price movement is
    # a range edge, not a trend. The smooth gate avoids a brittle hard cutoff.
    direction_gate = 1.0 / (1.0 + np.exp(-1.7 * (drift_t - 1.0)))

    # Structural trend: repeated tests and rejection matter more than the
    # distance of impulse-wave candles from the support/resistance envelope.
    raw = (0.26 * integrity + 0.12 * proximity + 0.22 * touch_score +
           0.22 * rejection + 0.08 * event_span +
           0.05 * efficiency + 0.05 * slope_strength)
    # Very short segments are easy to overfit; discount them smoothly.
    length_confidence = 0.78 + 0.22 * (1 - np.exp(-(n - 7) / 24))
    score = 100 * raw * length_confidence * (0.52 + 0.48 * direction_gate)

    return TrendResult(start, end, direction, slope,
                       intercept_local - slope * 0, score, integrity,
                       proximity, touches, touch_score, rejection, event_span,
                       efficiency, slope_strength, drift_t)


def fit_interval(df: pd.DataFrame, start: int, end: int,
                 direction: Direction) -> TrendResult | None:
    """Fit a lower/upper envelope by slope search plus asymmetric quantiles."""
    if end - start + 1 < 7:
        return None
    seg = df.iloc[start:end + 1]
    ref = anchor_points(seg, direction)
    n = len(ref)
    x = np.arange(n)
    d = 1 if direction == "up" else -1

    # Robust pairwise slopes. Subsampling caps cost for 150-candle windows.
    idx = np.unique(np.linspace(0, n - 1, min(n, 28)).astype(int))
    slopes = []
    for a, i in enumerate(idx[:-1]):
        j = idx[a + 1:]
        slopes.extend(((ref[j] - ref[i]) / (j - i)).tolist())
    slopes = np.asarray(slopes)
    directional = slopes[d * slopes > 0]
    if len(directional) < 3:
        return None
    qgrid = np.linspace(0.08, 0.92, 15)
    slope_grid = np.unique(np.quantile(directional, qgrid))
    intercept_quantiles = (0.07, 0.13, 0.20)

    # Cheap first pass: retain only a few envelope candidates. Full event
    # detection (local challenges and rebounds) is then run on those finalists.
    atr = np.maximum(true_range(df)[start:end + 1], 1e-9)
    shortlist = []
    for slope in slope_grid:
        residual = ref - slope * x
        for q in intercept_quantiles:
            iq = q if direction == "up" else 1 - q
            intercept = float(np.quantile(residual, iq))
            gap = d * (ref - (intercept + slope * x)) / atr
            coverage = np.mean(gap >= -0.22)
            # Cheap lower-envelope proxy for shortlisting. Using the all-candle
            # median here would again favor impulse legs over structural lines.
            proximity = np.exp(-np.quantile(np.abs(gap), 0.20) / 0.65)
            severe = np.mean(gap < -0.80)
            surrogate = 0.58 * coverage + 0.42 * proximity - 0.50 * severe
            shortlist.append((surrogate, float(slope), intercept))
    shortlist.sort(reverse=True)
    finalists = [_score_line(df, start, end, direction, slope, intercept)
                 for _, slope, intercept in shortlist[:5]]
    return max(finalists, key=lambda r: r.score)


def _candidate_pool(df: pd.DataFrame) -> list[TrendResult]:
    """Return a shared pool so nested tiers do not suppress one another."""
    n = len(df)
    base_lengths = [x for x in (7, 14, 30, 60, 90, 120, 150) if x <= n]
    seeds: list[TrendResult] = []
    for direction in ("up", "down"):
        for length in base_lengths:
            stride = 1 if length <= 14 else 3
            for end in range(length - 1, n, stride):
                r = fit_interval(df, end - length + 1, end, direction)
                if r:
                    seeds.append(r)

    # Refine both the global leaders and leaders in each direction/size bucket.
    # This prevents a strong long trend from starving medium candidates.
    seeds.sort(key=lambda z: z.score, reverse=True)
    refined = seeds[:40]
    seen = {(r.start, r.end, r.direction) for r in refined}
    short_max = max(14, int(round(0.30 * n)))
    long_min = max(short_max + 1, int(np.ceil(0.55 * n)))
    refinement_seeds = list(seeds[:10])
    for direction in ("up", "down"):
        buckets = (
            lambda r: r.length <= short_max,
            lambda r: short_max < r.length < long_min,
            lambda r: r.length >= long_min,
        )
        directional = [r for r in seeds if r.direction == direction]
        for predicate in buckets:
            refinement_seeds.extend([r for r in directional if predicate(r)][:3])

    unique_refinement = {}
    for r in refinement_seeds:
        unique_refinement[(r.start, r.end, r.direction)] = r
    for seed in unique_refinement.values():
        for ds in range(-7, 8, 3):
            for de in range(-7, 8, 3):
                s, e = max(0, seed.start + ds), min(n - 1, seed.end + de)
                key = (s, e, seed.direction)
                if e - s + 1 >= 7 and key not in seen:
                    seen.add(key)
                    r = fit_interval(df, s, e, seed.direction)
                    if r:
                        refined.append(r)

    # Search short lines whose fitted endpoint lies in the latest 10 bars. A
    # line may fit better before the newest pullback/bounce; it is projected to
    # today later and retained only if the post-fit bars did not break it.
    recent_ends = range(max(6, n - 10), n)
    recent_lengths = sorted(set([7, 10, 14, 20, 30, short_max]))
    recent_seeds = []
    for direction in ("up", "down"):
        for end in recent_ends:
            for length in recent_lengths:
                start = end - length + 1
                key = (start, end, direction)
                if start >= 0 and key not in seen:
                    seen.add(key)
                    r = fit_interval(df, start, end, direction)
                    if r:
                        refined.append(r)
                        recent_seeds.append(r)

    # Refine starts around the strongest recent coarse windows while keeping
    # the endpoint inside the latest-10 decision zone.
    recent_seeds.sort(key=lambda z: z.score, reverse=True)
    for seed in recent_seeds[:16]:
        for ds in range(-6, 7, 2):
            for de in range(-2, 3):
                s = max(0, seed.start + ds)
                e = min(n - 1, max(n - 10, seed.end + de))
                key = (s, e, seed.direction)
                if 7 <= e - s + 1 <= short_max and key not in seen:
                    seen.add(key)
                    r = fit_interval(df, s, e, seed.direction)
                    if r:
                        refined.append(r)

    refined.sort(key=lambda z: z.score, reverse=True)
    return refined


def _nms(candidates: list[TrendResult], max_results: int,
         overlap_limit: float = 0.60, score_fn=None) -> list[TrendResult]:
    """Suppress duplicates only inside one tier; cross-tier nesting is allowed."""
    selected: list[TrendResult] = []
    score_fn = score_fn or (lambda z: z.score)
    for r in sorted(candidates, key=score_fn, reverse=True):
        redundant = False
        for k in selected:
            inter = max(0, min(r.end, k.end) - max(r.start, k.start) + 1)
            union = r.length + k.length - inter
            if r.direction == k.direction and inter / union > overlap_limit:
                redundant = True
                break
        if not redundant:
            selected.append(r)
        if len(selected) >= max_results:
            break
    return selected


def search_trends(df: pd.DataFrame, threshold: float = 75.0,
                  max_results: int = 8) -> list[TrendResult]:
    """Backward-compatible flat output."""
    pool = [r for r in _candidate_pool(df) if r.score >= threshold]
    return _nms(pool, max_results=max_results)


def _latest_gap(df: pd.DataFrame, r: TrendResult) -> float:
    i = len(df) - 1
    d = 1 if r.direction == "up" else -1
    ref = anchor_points(df.iloc[[i]], r.direction)[0]
    return float(d * (ref - r.y(i)) / max(true_range(df)[i], 1e-9))


def _post_fit_gaps(df: pd.DataFrame, r: TrendResult) -> np.ndarray:
    """Evaluate an older short line by extrapolating it through today's bar."""
    indices = np.arange(r.end, len(df))
    segment = df.iloc[r.end:]
    d = 1 if r.direction == "up" else -1
    refs = anchor_points(segment, r.direction)
    atr = np.maximum(true_range(df)[r.end:], 1e-9)
    return d * (refs - r.y(indices)) / atr


def short_progress_score(r: TrendResult) -> float:
    """Tier-specific score: momentum/efficiency matter more than 3+ rejections."""
    significance = 1.0 / (1.0 + np.exp(-1.4 * (r.drift_t - 0.80)))
    return 100 * (
        0.22 * r.integrity + 0.18 * r.proximity +
        0.18 * r.efficiency + 0.15 * r.slope_strength +
        0.12 * significance + 0.07 * r.touch_score +
        0.04 * r.rejection + 0.04 * r.event_span
    )


def search_trend_hierarchy(
        df: pd.DataFrame,
        long_threshold: float = 75.0,
        medium_threshold: float = 70.0,
        short_threshold: float = 65.0,
) -> dict[Tier, list[TieredTrend]]:
    """Build nested long/medium/short output for a 150-candle decision chart.

    Long and medium tiers may include completed historical structures. A short
    fit may end within the latest 10 candles; it is extrapolated to today and
    returned only when no post-fit candle materially breaks it.
    """
    n = len(df)
    short_max = max(14, int(round(0.30 * n)))
    long_min = max(short_max + 1, int(np.ceil(0.55 * n)))
    pool = _candidate_pool(df)

    long_candidates = [
        r for r in pool if r.length >= long_min and r.score >= long_threshold
        and r.touches >= 3 and r.event_span >= 0.45
    ]
    medium_candidates = [
        r for r in pool if short_max < r.length < long_min
        and r.score >= medium_threshold and r.touches >= 2
        and r.event_span >= 0.35
    ]
    short_candidates = [
        r for r in pool if r.length <= short_max and 0 <= n - 1 - r.end <= 9
        and short_progress_score(r) >= short_threshold and r.touches >= 2
        and r.event_span >= 0.35 and r.drift_t >= 1.10
        and np.min(_post_fit_gaps(df, r)) >= -0.50
    ]

    chosen = {
        "long": _nms(long_candidates, max_results=3, overlap_limit=0.68),
        "medium": _nms(medium_candidates, max_results=4, overlap_limit=0.68),
        "short": _nms(short_candidates, max_results=2, overlap_limit=0.72,
                      score_fn=short_progress_score),
    }
    items: dict[Tier, list[TieredTrend]] = {"long": [], "medium": [], "short": []}
    for tier, values in chosen.items():
        for r in values:
            age = n - 1 - r.end
            current_gap = _latest_gap(df, r)
            if tier == "short":
                status = "challenging" if current_gap <= 0.50 else "valid"
                active = True  # broken/stale candidates were filtered out
                tier_score = short_progress_score(r)
            else:
                active = r.end == n - 1
                status = "current" if active else "historical"
                tier_score = r.score
            items[tier].append(TieredTrend(tier, r, active, None, status,
                                           age, current_gap, tier_score))

    # Attach each child to the smallest same-direction parent containing at
    # least 80% of the child. Orphans remain valid standalone trends.
    parents = items["long"] + items["medium"]
    for tier in ("medium", "short"):
        for child in items[tier]:
            eligible = []
            for parent in parents:
                if parent.tier == child.tier or parent.trend.direction != child.trend.direction:
                    continue
                inter = max(0, min(child.trend.end, parent.trend.end) -
                            max(child.trend.start, parent.trend.start) + 1)
                if inter / child.trend.length >= 0.80 and parent.trend.length > child.trend.length:
                    eligible.append(parent)
            if eligible:
                child.parent_id = min(eligible, key=lambda x: x.trend.length).id
    return items


def analyze_symbol_trendlines(symbol: str, period: str = "1D",
                              limit: int = 150,
                              show_weekend_data: str | bool | None = None) -> dict:
    """Analyze the latest candles for one symbol and return drawable lines."""
    clean_period = (period or "1D").upper()
    if clean_period not in SUPPORTED_PERIODS:
        raise ValueError("Unsupported analysis period")

    window_size = int(limit or 150)
    if window_size < 30 or window_size > 300:
        raise ValueError("Analysis window must be between 30 and 300 candles")

    payload = get_market_data(symbol)
    raw_rows = payload.get("data") or []
    include_weekends = resolve_show_weekend_data(
        show_weekend_data,
        payload.get("symbol_settings") or {},
    )
    if not include_weekends:
        raw_rows = [row for row in raw_rows if not is_weekend_date(row["date"])]
    candles = aggregate_rows(raw_rows, clean_period)
    if len(candles) < 7:
        return {
            "ok": True,
            "symbol": payload.get("symbol") or symbol,
            "canonical_symbol": payload.get("canonical_symbol") or symbol,
            "period": clean_period,
            "window_start_index": 0,
            "window_size": len(candles),
            "data_count": len(candles),
            "trends": [],
            "message": "K线数量不足，无法识别趋势线。",
        }

    window_start = max(0, len(candles) - window_size)
    window = candles[window_start:]
    df = candles_to_dataframe(window)
    hierarchy = search_trend_hierarchy(df)
    trends = serialize_hierarchy(hierarchy, window_start, len(candles) - 1)

    return {
        "ok": True,
        "symbol": payload.get("symbol") or symbol,
        "canonical_symbol": payload.get("canonical_symbol") or symbol,
        "source": payload.get("source"),
        "show_weekend_data": include_weekends,
        "period": clean_period,
        "window_start_index": window_start,
        "window_size": len(window),
        "data_count": len(candles),
        "trends": trends,
        "message": None if trends else "未识别出满足阈值的直线趋势线。",
    }


def resolve_show_weekend_data(value: str | bool | None, settings: dict) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(settings.get("show_weekend_data", True))


def is_weekend_date(date_text: str) -> bool:
    return datetime.strptime(date_text, "%Y-%m-%d").weekday() >= 5


def aggregate_rows(rows: list[dict], period: str) -> list[dict]:
    clean_rows = [normalize_ohlcv_row(row) for row in rows]
    if period == "1D":
        return clean_rows
    if period == "3D":
        return [
            merged
            for index in range(0, len(clean_rows), 3)
            if (merged := merge_ohlcv_rows(clean_rows[index:index + 3]))
        ]
    return aggregate_rows_by_calendar(clean_rows, period)


def normalize_ohlcv_row(row: dict) -> dict:
    return {
        "date": row["date"],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row.get("volume") or 0),
    }


def aggregate_rows_by_calendar(rows: list[dict], period: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = week_key(row["date"]) if period == "1W" else row["date"][:7]
        groups.setdefault(key, []).append(row)
    return [merged for values in groups.values() if (merged := merge_ohlcv_rows(values))]


def merge_ohlcv_rows(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    first = rows[0]
    last = rows[-1]
    return {
        "date": first["date"],
        "end_date": last["date"],
        "open": first["open"],
        "high": max(row["high"] for row in rows),
        "low": min(row["low"] for row in rows),
        "close": last["close"],
        "volume": sum(row["volume"] for row in rows),
    }


def week_key(date_text: str) -> str:
    value = datetime.strptime(date_text, "%Y-%m-%d").date()
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [row["open"] for row in candles],
            "High": [row["high"] for row in candles],
            "Low": [row["low"] for row in candles],
            "Close": [row["close"] for row in candles],
            "Volume": [row["volume"] for row in candles],
        }
    )


def serialize_hierarchy(hierarchy: dict[Tier, list[TieredTrend]],
                        window_start: int, latest_index: int) -> list[dict]:
    result = []
    for tier in ("long", "medium", "short"):
        for item in hierarchy[tier]:
            result.append(serialize_trend(item, window_start, latest_index))
    return result


def serialize_trend(item: TieredTrend, window_start: int,
                    latest_index: int) -> dict:
    trend = item.trend
    start_index = window_start + trend.start
    end_index = window_start + trend.end
    projection_end_index = latest_index if item.tier == "short" else end_index
    local_projection_end = projection_end_index - window_start
    start_price = float(trend.y(trend.start))
    end_price = float(trend.y(trend.end))
    projection_end_price = float(trend.y(local_projection_end))
    return {
        "id": item.id,
        "tier": item.tier,
        "direction": trend.direction,
        "start_index": start_index,
        "end_index": end_index,
        "projection_end_index": projection_end_index,
        "start_price": start_price,
        "end_price": end_price,
        "projection_end_price": projection_end_price,
        "slope": float(trend.slope),
        "intercept": float(trend.intercept),
        "score": round(float(trend.score), 4),
        "tier_score": round(float(item.tier_score), 4),
        "integrity": round(float(trend.integrity), 4),
        "proximity": round(float(trend.proximity), 4),
        "touches": int(trend.touches),
        "touch_score": round(float(trend.touch_score), 4),
        "rejection": round(float(trend.rejection), 4),
        "event_span": round(float(trend.event_span), 4),
        "efficiency": round(float(trend.efficiency), 4),
        "slope_strength": round(float(trend.slope_strength), 4),
        "drift_t": round(float(trend.drift_t), 4),
        "active": bool(item.active),
        "parent_id": item.parent_id,
        "status": item.status,
        "age": int(item.age),
        "current_gap": round(float(item.current_gap), 4),
    }


def _candles(ax, df: pd.DataFrame) -> None:
    for i, row in df.reset_index(drop=True).iterrows():
        color = "#d64b4b" if row.Close >= row.Open else "#2f9e6f"
        ax.vlines(i, row.Low, row.High, color=color, linewidth=0.8, alpha=0.85)
        bottom, height = min(row.Open, row.Close), abs(row.Close - row.Open)
        ax.add_patch(Rectangle((i - 0.32, bottom), 0.64, max(height, 0.025),
                               facecolor=color, edgecolor=color, linewidth=0.5))


def plot_case(df: pd.DataFrame, trends: list[TrendResult], title: str,
              path: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 5.3))
    _candles(ax, df)
    colors = {"up": "#2563eb", "down": "#e8790b"}
    for j, r in enumerate(trends[:4]):
        x = np.arange(r.start, r.end + 1)
        ax.plot(x, r.y(x), color=colors[r.direction], linewidth=2.2,
                label=f"{r.direction} {r.start + 1}-{r.end + 1}, score={r.score:.1f}")
        ax.scatter([r.start, r.end], r.y(np.array([r.start, r.end])),
                   color=colors[r.direction], s=18, zorder=5)
    ax.set_title(title)
    ax.set_xlabel("Candle index")
    ax.set_ylabel("Price")
    ax.grid(alpha=0.16)
    if trends:
        ax.legend(loc="best", frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_hierarchy_case(df: pd.DataFrame,
                        hierarchy: dict[Tier, list[TieredTrend]],
                        title: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 5.3))
    _candles(ax, df)
    colors = {
        ("long", "up"): "#1d4ed8", ("long", "down"): "#d97706",
        ("medium", "up"): "#0891b2", ("medium", "down"): "#dc2626",
        ("short", "up"): "#7c3aed", ("short", "down"): "#c026d3",
    }
    styles = {"long": ("-", 2.8), "medium": ("--", 2.1),
              "short": (":", 2.8)}
    names = {"long": "L", "medium": "M", "short": "S-now"}
    # Limit the visual layer while retaining all selected items in CSV output.
    visual_limits = {"long": 2, "medium": 2, "short": 2}
    plotted = 0
    for tier in ("long", "medium", "short"):
        linestyle, width = styles[tier]
        for item in hierarchy[tier][:visual_limits[tier]]:
            r = item.trend
            x = np.arange(r.start, r.end + 1)
            parent = f", parent={item.parent_id}" if item.parent_id else ""
            ax.plot(x, r.y(x), color=colors[(tier, r.direction)],
                    linestyle=linestyle, linewidth=width,
                    label=(f"{names[tier]} {r.direction} {r.start + 1}-{r.end + 1}, "
                           f"score={item.tier_score:.1f}, age={item.age}, "
                           f"{item.status}{parent}"))
            if tier == "short" and r.end < len(df) - 1:
                projected_x = np.arange(r.end, len(df))
                ax.plot(projected_x, r.y(projected_x),
                        color=colors[(tier, r.direction)], linestyle=":",
                        linewidth=1.7, alpha=0.48)
            plotted += 1
    ax.set_title(title)
    ax.set_xlabel("Candle index")
    ax.set_ylabel("Price")
    ax.grid(alpha=0.16)
    if plotted:
        ax.legend(loc="best", frameon=False, ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def synthetic_ohlc(kind: str, n: int = 150, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    if kind == "clean_up":
        base = 100 + 0.23 * t + 1.35 * np.abs(np.sin(t / 6.2))
        close = base + rng.normal(0, 0.34, n)
    elif kind == "clean_down":
        base = 140 - 0.20 * t - 1.25 * np.abs(np.sin(t / 6.8))
        close = base + rng.normal(0, 0.36, n)
    elif kind == "regime_switch":
        close = np.empty(n)
        close[:88] = 95 + 0.27 * t[:88] + 1.2 * np.abs(np.sin(t[:88] / 6))
        close[88:] = close[87] - 0.33 * np.arange(1, n - 87) - 1.1 * np.abs(np.sin(np.arange(n - 88) / 5))
        close += rng.normal(0, 0.38, n)
    elif kind == "wave_up":
        close = 100 + 0.13 * t + 3.0 * np.sin(t / 8.5) + rng.normal(0, 0.42, n)
    elif kind == "wave_down":
        close = 140 - 0.13 * t + 3.0 * np.sin(t / 8.5) + rng.normal(0, 0.42, n)
    elif kind == "irregular_wave_up":
        phase = t / 9.2 + 0.00075 * t ** 2
        amplitude = 2.4 + 0.65 * np.sin(t / 31)
        close = 96 + 0.145 * t + amplitude * np.sin(phase) + rng.normal(0, 0.46, n)
    elif kind == "irregular_wave_down":
        phase = t / 9.0 + 0.00065 * t ** 2
        amplitude = 2.5 + 0.60 * np.cos(t / 29)
        close = 142 - 0.14 * t + amplitude * np.sin(phase) + rng.normal(0, 0.46, n)
    elif kind == "wave_up_false_break":
        base = 98 + 0.14 * t + 2.8 * np.sin(t / 8.7)
        # A temporary downside overshoot close to a cyclical trough.
        shock = -1.35 * np.exp(-0.5 * ((t - 91) / 1.8) ** 2)
        close = base + shock + rng.normal(0, 0.40, n)
    elif kind == "nested_wave_up":
        close = (97 + 0.135 * t + 2.7 * np.sin(t / 9.0) +
                 0.65 * np.sin(t / 2.15) + rng.normal(0, 0.36, n))
    elif kind == "sideways_wave":
        close = 110 + 3.0 * np.sin(t / 8.5) + rng.normal(0, 0.42, n)
    elif kind == "sideways":
        close = 110 + np.cumsum(rng.normal(0, 0.48, n))
        close = 110 + 0.82 * (close - pd.Series(close).rolling(18, min_periods=1).mean())
    elif kind == "accelerating":
        close = 90 + 0.045 * t + 0.0017 * t ** 2 + rng.normal(0, 0.45, n)
    elif kind == "late_breakdown":
        close = 98 + 0.17 * t + 2.2 * np.sin(t / 8.8)
        pivot = 132
        close[pivot:] = close[pivot - 1] - 0.82 * np.arange(1, n - pivot + 1)
        close += rng.normal(0, 0.48, n)
    elif kind == "gap_reversal":
        close = 100 + 0.19 * t + 1.3 * np.sin(t / 6.5)
        pivot = 91
        close[pivot:] = (close[pivot - 1] - 7.5 -
                         0.24 * np.arange(0, n - pivot) +
                         1.0 * np.sin(np.arange(n - pivot) / 5.0))
        close += rng.normal(0, 0.42, n)
    elif kind == "volatility_shift":
        sigma = np.where(t < 82, 0.28, 1.15)
        close = (96 + 0.14 * t + 2.5 * np.sin(t / 9.0) +
                 rng.normal(0, sigma, n))
    elif kind == "blowoff_reversal":
        close = 92 + 0.055 * t + 0.0022 * t ** 2
        pivot = 128
        peak = close[pivot - 1]
        close[pivot:] = peak - 1.35 * np.arange(1, n - pivot + 1)
        close += rng.normal(0, 0.55, n)
    elif kind == "flash_break_recovery":
        close = 99 + 0.145 * t + 2.4 * np.sin(t / 8.7)
        shock = -6.5 * np.exp(-0.5 * ((t - 113) / 1.05) ** 2)
        close += shock + rng.normal(0, 0.42, n)
    else:
        raise ValueError(kind)

    open_ = np.r_[close[0] + rng.normal(0, .25), close[:-1] + rng.normal(0, .25, n - 1)]
    body_hi, body_lo = np.maximum(open_, close), np.minimum(open_, close)
    high = body_hi + rng.uniform(0.25, 0.95, n)
    low = body_lo - rng.uniform(0.25, 0.95, n)
    volume = rng.integers(700_000, 1_400_000, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low,
                         "Close": close, "Volume": volume})


def main() -> None:
    out = Path("trendline_results")
    out.mkdir(exist_ok=True)
    cases = ["clean_up", "clean_down", "wave_up", "wave_down",
             "irregular_wave_up", "irregular_wave_down",
             "wave_up_false_break", "nested_wave_up",
             "sideways_wave", "regime_switch", "sideways", "accelerating"]
    rows = []
    for k, case in enumerate(cases):
        df = synthetic_ohlc(case, seed=11 + k)
        hierarchy = search_trend_hierarchy(df)
        plot_hierarchy_case(df, hierarchy, case.replace("_", " ").title(),
                            out / f"{case}_hierarchy.png")
        for tier in ("long", "medium", "short"):
            for rank, item in enumerate(hierarchy[tier], 1):
                r = item.trend
                row = {"case": case, "tier": tier, "rank": rank,
                       "id": item.id, "parent_id": item.parent_id,
                       "active": item.active, "status": item.status,
                       "age": item.age, "current_gap": item.current_gap,
                       "tier_score": item.tier_score,
                       **asdict(r), "length": r.length}
                rows.append(row)
    result = pd.DataFrame(rows)
    result.to_csv(out / "hierarchy_scores.csv", index=False)
    if len(result):
        print(result[["case", "tier", "rank", "active", "parent_id",
                      "direction", "start", "end", "length",
                      "score", "touches", "integrity", "proximity",
                      "rejection"]].to_string(index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
