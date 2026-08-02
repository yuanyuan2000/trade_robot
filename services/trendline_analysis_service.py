"""Straight support/resistance trend-line detector for OHLC candles.

The detector is deliberately written as a transparent research prototype:
every score component is returned, so thresholds can be calibrated on real
market data rather than treated as immutable constants.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import threading
from typing import Literal

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from services.market_data_service import get_market_data


Direction = Literal["up", "down"]
Tier = Literal["long", "medium", "short"]
SUPPORTED_PERIODS = {"1D", "3D", "1W", "1M"}
ANALYSIS_CACHE_VERSION = "trendline-v13-flat-line-veto"
ANALYSIS_CACHE_MAX_SIZE = 64
TOUCH_DISTANCE_ATR = 0.75
FLAT_LINE_MAX_SLOPE_ATR_PER_20 = 0.45
FLAT_LINE_MAX_ENDPOINT_MOVE_RATIO = 0.10
TOUCH_DISTRIBUTION_FULL_QUALITY = 0.55
TOUCH_DISTRIBUTION_PENALTY = {
    "long": 0.16,
    "medium": 0.14,
    "short": 0.08,
}
CHALLENGE_DISTANCE_ATR = 0.50
BREAK_CLOSE_DISTANCE_ATR = 0.30
SEVERE_BREAK_CLOSE_DISTANCE_ATR = 0.80
ACCELERATION_END_DISTANCE_ATR = 4.00
ACCELERATION_REENTRY_DISTANCE_ATR = 1.50
ACCELERATION_CONFIRM_BARS = 3
TREND_FAMILY_MIN_OVERLAP = 0.55
TREND_FAMILY_MAX_MEDIAN_DISTANCE_ATR = 2.00
TREND_FAMILY_MAX_DISTANCE_80_ATR = 3.00
TREND_FAMILY_NOVEL_TOUCH_DISTANCE_ATR = 0.75
TREND_FAMILY_SEPARATION_DISTANCE_ATR = 0.80
TREND_FAMILY_MIN_SEPARATION_RUN = 8
TREND_FAMILY_MIN_SLOPE_DIFFERENCE = 0.25
TREND_FAMILY_MAX_STAGE_SCORE_GAP = 8.00
_analysis_result_cache: OrderedDict[tuple, tuple[dict, ...]] = OrderedDict()
_analysis_result_cache_lock = threading.Lock()


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
    touch_distribution: float
    max_touch_gap: float
    interior_touches: int
    first_touch: int
    last_touch: int
    touch_indices: tuple[int, ...]
    body_integrity: float
    body_breach_ratio: float
    severe_body_breach_ratio: float
    max_body_breach_run: int
    efficiency: float
    slope_strength: float
    drift_t: float

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def structure_length(self) -> int:
        return self.last_touch - self.first_touch + 1

    def y(self, absolute_index: np.ndarray | float) -> np.ndarray:
        return self.intercept + self.slope * (np.asarray(absolute_index) - self.start)


@dataclass
class TieredTrend:
    tier: Tier
    trend: TrendResult
    active: bool
    parent_id: str | None = None
    status: str = "trending"
    age: int = 0
    current_gap: float = 0.0
    previous_gap: float = 0.0
    tier_score: float = 0.0
    break_index: int | None = None
    acceleration_index: int | None = None
    family_id: str | None = None
    family_role: str = "standalone"
    score_formula: str = ""

    @property
    def id(self) -> str:
        r = self.trend
        return (
            f"{self.tier[0].upper()}-{r.direction}-"
            f"{r.first_touch + 1}-{r.last_touch + 1}"
        )


@dataclass(frozen=True)
class ContactMetrics:
    touches: int
    touch_score: float
    rejection: float
    proximity: float
    event_span: float
    touch_distribution: float
    max_touch_gap: float
    interior_touches: int
    first_touch: int
    last_touch: int
    touch_indices: tuple[int, ...]


@dataclass(frozen=True)
class BodyMetrics:
    integrity: float
    breach_ratio: float
    severe_breach_ratio: float
    max_breach_run: int


@dataclass(frozen=True)
class TrendFamilyMetrics:
    overlap_ratio: float
    median_distance: float
    distance_80: float
    slope_difference: float
    novel_touches: int
    separation_run: int


@dataclass(frozen=True)
class AnalysisArrays:
    close: np.ndarray
    atr: np.ndarray
    anchor_up: np.ndarray
    anchor_down: np.ndarray
    body_edge_up: np.ndarray
    body_edge_down: np.ndarray

    def anchor(self, direction: Direction) -> np.ndarray:
        return self.anchor_up if direction == "up" else self.anchor_down

    def body_edge(self, direction: Direction) -> np.ndarray:
        return self.body_edge_up if direction == "up" else self.body_edge_down


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


def _analysis_arrays(df: pd.DataFrame) -> AnalysisArrays:
    cached = df.attrs.get("_trendline_analysis_arrays")
    if cached is not None and len(cached.close) == len(df):
        return cached

    open_values = df["Open"].to_numpy()
    close_values = df["Close"].to_numpy()
    low_values = df["Low"].to_numpy()
    high_values = df["High"].to_numpy()
    body_edge_up = np.minimum(open_values, close_values)
    body_edge_down = np.maximum(open_values, close_values)
    arrays = AnalysisArrays(
        close=close_values,
        atr=true_range(df),
        anchor_up=0.75 * body_edge_up + 0.25 * (
            (low_values + body_edge_up) / 2
        ),
        anchor_down=0.75 * body_edge_down + 0.25 * (
            (high_values + body_edge_down) / 2
        ),
        body_edge_up=body_edge_up,
        body_edge_down=body_edge_down,
    )
    df.attrs["_trendline_analysis_arrays"] = arrays
    return arrays


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


def body_edges(df: pd.DataFrame, direction: Direction) -> np.ndarray:
    open_values = df["Open"].to_numpy()
    close_values = df["Close"].to_numpy()
    if direction == "up":
        return np.minimum(open_values, close_values)
    return np.maximum(open_values, close_values)


def _longest_true_run(mask: np.ndarray) -> int:
    best = current = 0
    for value in mask:
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def _body_metrics(body_gap: np.ndarray) -> BodyMetrics:
    """Measure line penetration into candle bodies in ATR units."""
    breached = body_gap < -0.10
    severe = body_gap < -0.35
    max_run = _longest_true_run(breached)
    penetration = float(np.mean(np.clip(-body_gap - 0.06, 0, None)))
    breach_ratio = float(np.mean(breached))
    severe_ratio = float(np.mean(severe))
    integrity = float(np.exp(
        -4.0 * penetration -
        4.0 * severe_ratio -
        0.75 * max(0, max_run - 1)
    ))
    return BodyMetrics(integrity, breach_ratio, severe_ratio, max_run)


def _body_integrity_batch(body_gap: np.ndarray) -> np.ndarray:
    """Vectorized entity integrity for a candidate-by-candle matrix."""
    breached = body_gap < -0.10
    severe_ratio = np.mean(body_gap < -0.35, axis=1)
    penetration = np.mean(np.clip(-body_gap - 0.06, 0, None), axis=1)
    current_run = np.zeros(len(body_gap), dtype=np.int32)
    max_run = np.zeros(len(body_gap), dtype=np.int32)
    for column in breached.T:
        current_run = np.where(column, current_run + 1, 0)
        max_run = np.maximum(max_run, current_run)
    return np.exp(
        -4.0 * penetration
        -4.0 * severe_ratio
        -0.75 * np.maximum(0, max_run - 1)
    )


def _event_metrics(
        gap: np.ndarray,
        wick_gap: np.ndarray | None = None,
        body_gap: np.ndarray | None = None) -> ContactMetrics:
    """Find scale-aware support/resistance tests and subsequent rejections.

    gap is positive on the valid side of either support or resistance, in ATR.
    A small negative gap is therefore a tolerated false break for both cases.

    Crucially, closeness is measured at structural pullback/rebound pivots, not
    on every candle. Peaks are allowed to be far from a support line and troughs
    are allowed to be far from a resistance line.
    """
    n = len(gap)
    smooth_window = 3 if n < 36 else 5
    smooth = (pd.Series(gap).rolling(smooth_window, center=True, min_periods=1)
              .mean().to_numpy())
    min_distance = max(2, n // 18)
    pivots, properties = find_peaks(
        -smooth,
        distance=min_distance,
        prominence=0.12,
    )
    prominences = properties.get("prominences", np.zeros(len(pivots)))
    use_raw_contacts = wick_gap is not None and body_gap is not None
    if use_raw_contacts:
        wick_gap = np.asarray(wick_gap, dtype=float)
        body_gap = np.asarray(body_gap, dtype=float)
        # A wick can confirm the same boundary provided the candle body did not
        # severely cross it. Keep this channel raw so a one-bar rejection is
        # not diluted by the structural smoother.
        valid_body = body_gap >= -0.35
        anchor_signal = np.where(valid_body, gap, np.inf)
        wick_signal = np.where(valid_body, wick_gap, np.inf)
        contact_signal = np.minimum(anchor_signal, wick_signal)
    else:
        contact_signal = smooth
    raw_radius = max(1, smooth_window // 2)
    candidate_map: dict[int, float] = {}

    def add_candidate(center: int, prominence: float) -> None:
        contact = center
        if use_raw_contacts:
            local_start = max(0, center - raw_radius)
            local_end = min(n, center + raw_radius + 1)
            local = contact_signal[local_start:local_end]
            if not len(local) or not np.any(np.isfinite(local)):
                return
            contact = local_start + int(np.nanargmin(local))
        if contact_signal[contact] > TOUCH_DISTANCE_ATR:
            return
        nearby = [
            index
            for index in candidate_map
            if abs(index - contact) < min_distance
        ]
        if nearby:
            incumbent = min(
                nearby,
                key=lambda index: contact_signal[index],
            )
            if contact_signal[incumbent] <= contact_signal[contact]:
                candidate_map[incumbent] = max(
                    candidate_map[incumbent],
                    float(prominence),
                )
                return
            for index in nearby:
                candidate_map.pop(index, None)
        candidate_map[contact] = float(prominence)

    for pivot, prominence in zip(pivots, prominences):
        add_candidate(int(pivot), float(prominence))

    horizon = min(24, max(7, n // 6))

    # scipy's peak finder intentionally excludes endpoints. Both edges matter:
    # an older trend often begins at its first rejection, while a live trend may
    # end with an unconfirmed challenge near today's candle.
    edge_width = max(4, n // 15)
    edge_ranges = (
        range(0, min(n, edge_width)),
        range(max(0, n - edge_width), n),
    )
    for edge_range in edge_ranges:
        edge_values = list(edge_range)
        if not edge_values:
            continue
        edge_i = min(edge_values, key=lambda index: contact_signal[index])
        if contact_signal[edge_i] > TOUCH_DISTANCE_ATR:
            continue
        local_start = max(0, edge_i - horizon)
        local_end = min(n, edge_i + horizon + 1)
        local_high = np.max(smooth[local_start:local_end])
        add_candidate(
            edge_i,
            max(0.0, float(local_high - smooth[edge_i])),
        )

    ordered = sorted(candidate_map.items())
    candidates = [item[0] for item in ordered]
    kept_prominence = [item[1] for item in ordered]

    evaluated, qualities = 0, []
    for i, prominence in zip(candidates, kept_prominence):
        future = smooth[i + 1:min(n, i + horizon + 1)]
        if len(future) < 2:
            continue
        evaluated += 1
        contact_distance = contact_signal[i]
        rebound = np.max(future) - contact_distance
        stayed_intact = np.min(future) > -0.85
        q_forward = np.clip(rebound / 1.50, 0, 1)
        q_prominence = np.clip(prominence / 1.20, 0, 1)
        q = (0.70 * q_forward + 0.30 * q_prominence)
        q *= 1.0 if stayed_intact else 0.20
        # A shallow penetration followed by rejection is especially strong.
        if -0.45 <= contact_distance < 0 and stayed_intact:
            q = min(1.0, q + 0.12)
        qualities.append(q)

    touch_count = len(candidates)
    count_scores = (0.0, 0.12, 0.43, 0.73, 0.88, 1.0)
    count_score = count_scores[min(touch_count, len(count_scores) - 1)]
    event_span = 0.0
    touch_distribution = 0.0
    max_touch_gap = 1.0
    interior_touches = 0
    if touch_count >= 2:
        event_span = (candidates[-1] - candidates[0]) / max(1, n - 1)
        relative = (
            (np.asarray(candidates, dtype=float) - candidates[0]) /
            max(1, candidates[-1] - candidates[0])
        )
        max_touch_gap = float(np.max(np.diff(relative)))
        occupied_bins = len(set(np.minimum(3, (relative * 4).astype(int))))
        gap_score = float(np.clip((0.78 - max_touch_gap) / 0.38, 0, 1))
        bin_score = float(np.clip((occupied_bins - 1) / 3, 0, 1))
        touch_distribution = 0.65 * gap_score + 0.35 * bin_score
        interior_touches = sum(
            0.20 <= i / max(1, n - 1) <= 0.80 for i in candidates
        )
    span_factor = 0.62 + 0.38 * min(1.0, event_span / 0.60)
    distribution_factor = 0.72 + 0.28 * touch_distribution
    touch_score = count_score * span_factor * distribution_factor
    rejection = float(np.mean(qualities)) if evaluated else 0.0

    if candidates:
        # Distance only at pullback pivots defines lower/upper-envelope fit.
        pivot_distance = np.median(np.abs(contact_signal[candidates]))
        envelope_proximity = float(np.exp(-pivot_distance / 0.65))
    else:
        # No challenges means weak evidence even if the line is geometrically
        # below/above all candles.
        envelope_proximity = 0.0
    return ContactMetrics(
        touches=touch_count,
        touch_score=float(touch_score),
        rejection=rejection,
        proximity=envelope_proximity,
        event_span=float(event_span),
        touch_distribution=float(touch_distribution),
        max_touch_gap=max_touch_gap,
        interior_touches=interior_touches,
        first_touch=candidates[0] if candidates else 0,
        last_touch=candidates[-1] if candidates else n - 1,
        touch_indices=tuple(candidates),
    )


def _score_line(df: pd.DataFrame, start: int, end: int, direction: Direction,
                slope: float, intercept_local: float) -> TrendResult:
    arrays = _analysis_arrays(df)
    n = end - start + 1
    x = np.arange(n)
    d = 1 if direction == "up" else -1
    atr = np.maximum(arrays.atr[start:end + 1], 1e-9)
    ref = arrays.anchor(direction)[start:end + 1]
    line = intercept_local + slope * x
    gap = d * (ref - line) / atr
    body_gap = d * (
        arrays.body_edge(direction)[start:end + 1] - line
    ) / atr

    tolerated = 0.22
    coverage = np.mean(gap >= -tolerated)
    soft_breach = np.mean(np.clip(-gap - tolerated, 0, None))
    hard_breach = np.mean(gap < -0.80)
    integrity = np.clip((coverage - 0.72) / 0.27, 0, 1)
    integrity *= np.exp(-2.5 * soft_breach - 3.0 * hard_breach)

    contacts = _event_metrics(gap)
    body = _body_metrics(body_gap)

    close = arrays.close[start:end + 1]
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
    raw = (
        0.17 * integrity +
        0.18 * body.integrity +
        0.11 * contacts.proximity +
        0.22 * contacts.touch_score +
        0.14 * contacts.rejection +
        0.10 * contacts.touch_distribution +
        0.03 * efficiency +
        0.05 * slope_strength
    )
    # Very short segments are easy to overfit; discount them smoothly.
    length_confidence = 0.86 + 0.14 * (1 - np.exp(-(n - 7) / 20))
    score = 100 * raw * length_confidence * (0.68 + 0.32 * direction_gate)

    return TrendResult(start, end, direction, slope,
                       intercept_local - slope * 0, score, integrity,
                       contacts.proximity, contacts.touches,
                       contacts.touch_score, contacts.rejection,
                       contacts.event_span, contacts.touch_distribution,
                       contacts.max_touch_gap, contacts.interior_touches,
                       start + contacts.first_touch,
                       start + contacts.last_touch,
                       tuple(start + i for i in contacts.touch_indices),
                       body.integrity, body.breach_ratio,
                       body.severe_breach_ratio, body.max_breach_run,
                       efficiency, slope_strength, drift_t)


def _long_score_from_metrics(r: TrendResult) -> float:
    raw = (
        0.17 * r.integrity +
        0.18 * r.body_integrity +
        0.11 * r.proximity +
        0.22 * r.touch_score +
        0.14 * r.rejection +
        0.10 * r.touch_distribution +
        0.03 * r.efficiency +
        0.05 * r.slope_strength
    )
    length_confidence = 0.86 + 0.14 * (
        1 - np.exp(-(r.length - 7) / 20)
    )
    direction_gate = 1.0 / (
        1.0 + np.exp(-1.7 * (r.drift_t - 1.0))
    )
    return float(
        100 * raw * length_confidence * (0.68 + 0.32 * direction_gate)
    )


def fit_interval(df: pd.DataFrame, start: int, end: int,
                 direction: Direction) -> TrendResult | None:
    """Fit a lower/upper envelope by slope search plus asymmetric quantiles."""
    if end - start + 1 < 7:
        return None
    arrays = _analysis_arrays(df)
    ref = arrays.anchor(direction)[start:end + 1]
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
    intercept_quantiles = (0.03, 0.07, 0.13, 0.20)

    # Cheap first pass: retain only a few envelope candidates. Full event
    # detection (local challenges and rebounds) is then run on those finalists.
    atr = np.maximum(arrays.atr[start:end + 1], 1e-9)
    bodies = arrays.body_edge(direction)[start:end + 1]
    residuals = ref[None, :] - slope_grid[:, None] * x
    intercept_levels = (
        np.asarray(intercept_quantiles)
        if direction == "up"
        else 1 - np.asarray(intercept_quantiles)
    )
    intercept_grid = np.quantile(
        residuals,
        intercept_levels,
        axis=1,
    ).T.reshape(-1)
    candidate_slopes = np.repeat(slope_grid, len(intercept_quantiles))
    lines = (
        intercept_grid[:, None]
        + candidate_slopes[:, None] * x[None, :]
    )
    gap = d * (ref[None, :] - lines) / atr[None, :]
    coverage = np.mean(gap >= -0.22, axis=1)
    # Cheap lower-envelope proxy for shortlisting. Using the all-candle
    # median here would again favor impulse legs over structural lines.
    proximity = np.exp(np.quantile(np.abs(gap), 0.20, axis=1) / -0.65)
    severe = np.mean(gap < -0.80, axis=1)
    body_gap = d * (bodies[None, :] - lines) / atr[None, :]
    body_integrity = _body_integrity_batch(body_gap)
    surrogate = (
        0.45 * coverage
        + 0.30 * proximity
        + 0.25 * body_integrity
        - 0.50 * severe
    )
    shortlist = list(zip(
        surrogate.tolist(),
        candidate_slopes.tolist(),
        intercept_grid.tolist(),
    ))
    shortlist.sort(reverse=True)
    finalists = [_score_line(df, start, end, direction, slope, intercept)
                 for _, slope, intercept in shortlist[:5]]
    short_max, long_min = _tier_boundaries(len(df))
    if n <= short_max:
        score_fn = short_progress_score
    elif n < long_min:
        score_fn = medium_trend_score
    else:
        score_fn = lambda result: result.score
    return max(finalists, key=score_fn)


def _tier_boundaries(n: int) -> tuple[int, int]:
    """Return inclusive short maximum and long minimum for the analysis size."""
    short_max = max(10, int(round(0.10 * n)))
    long_min = max(short_max + 1, int(round(n / 3)))
    return short_max, long_min


def _tier_for_length(length: int, n: int) -> Tier:
    short_max, long_min = _tier_boundaries(n)
    if length <= short_max:
        return "short"
    if length < long_min:
        return "medium"
    return "long"


def medium_trend_score(r: TrendResult) -> float:
    """Balance directional progress with repeated envelope confirmation."""
    significance = 1.0 / (1.0 + np.exp(-1.35 * (r.drift_t - 0.65)))
    length_confidence = 0.90 + 0.10 * (
        1 - np.exp(-(max(0, r.length - 12)) / 18)
    )
    raw = (
        0.17 * r.integrity +
        0.18 * r.body_integrity +
        0.12 * r.proximity +
        0.20 * r.touch_score +
        0.12 * r.rejection +
        0.09 * r.touch_distribution +
        0.06 * r.efficiency +
        0.04 * r.slope_strength +
        0.02 * significance
    )
    return 100 * raw * length_confidence


def _candidate_rank(r: TrendResult, n: int) -> float:
    tier = _tier_for_length(r.structure_length, n)
    if tier == "short":
        score = short_progress_score(r)
    elif tier == "medium":
        score = _medium_selection_score(r, n)
    else:
        score = r.score
    return _apply_distribution_penalty(tier, r, score)


def _coarse_lengths(n: int) -> list[int]:
    short_max, long_min = _tier_boundaries(n)
    values = {
        7,
        10,
        short_max,
        int(round(0.15 * n)),
        int(round(0.21 * n)),
        int(round(0.30 * n)),
        int(round(0.42 * n)),
        long_min + 9,
        int(round(0.57 * n)),
        int(round(0.77 * n)),
        n,
    }
    return sorted(length for length in values if 7 <= length <= n)


def _candidate_pool(df: pd.DataFrame) -> list[TrendResult]:
    """Return a time-balanced shared candidate pool for every duration tier."""
    n = len(df)
    short_max, long_min = _tier_boundaries(n)
    base_lengths = _coarse_lengths(n)
    seeds: list[TrendResult] = []
    for direction in ("up", "down"):
        for length in base_lengths:
            stride = 1 if length <= 10 else (2 if length < long_min else 3)
            for end in range(length - 1, n, stride):
                r = fit_interval(df, end - length + 1, end, direction)
                if r:
                    seeds.append(r)

    # Keep global leaders plus local leaders in each direction, duration and
    # chart region. Time balancing prevents a strong recent regime from
    # starving a visually clear historical segment.
    seeds.sort(key=lambda z: _candidate_rank(z, n), reverse=True)
    retained = list(seeds[:36])
    region_size = max(1, int(np.ceil(n / 5)))
    grouped: dict[tuple, list[TrendResult]] = {}
    for r in seeds:
        key = (
            r.direction,
            _tier_for_length(r.structure_length, n),
            min(4, r.start // region_size),
            min(4, r.end // region_size),
        )
        grouped.setdefault(key, []).append(r)
    for values in grouped.values():
        retained.extend(values[:2])

    unique_retained = {
        (r.start, r.end, r.direction): r
        for r in retained
    }
    refined = list(unique_retained.values())
    seen = {(r.start, r.end, r.direction) for r in refined}
    refinement_seeds = sorted(
        unique_retained.values(),
        key=lambda z: _candidate_rank(z, n),
        reverse=True,
    )[:42]
    for seed in refinement_seeds:
        for ds in range(-6, 7, 3):
            for de in range(-6, 7, 3):
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
    recent_max = max(short_max, min(30, n))
    recent_lengths = sorted(set([7, 10, short_max, 20, 24, recent_max]))
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
                if 7 <= e - s + 1 <= recent_max and key not in seen:
                    seen.add(key)
                    r = fit_interval(df, s, e, seed.direction)
                    if r:
                        refined.append(r)

    refined.sort(key=lambda z: _candidate_rank(z, n), reverse=True)
    return refined


def _lines_are_duplicates(
        df: pd.DataFrame,
        first: TrendResult,
        second: TrendResult,
) -> bool:
    """Identify only genuinely coincident lines, independent of duration tier."""
    if first.direction != second.direction:
        return False
    overlap_start = max(first.first_touch, second.first_touch)
    overlap_end = min(first.last_touch, second.last_touch)
    overlap = overlap_end - overlap_start + 1
    if overlap <= 1:
        return False
    overlap_ratio = overlap / min(
        first.structure_length,
        second.structure_length,
    )
    dates_close = (
        abs(first.first_touch - second.first_touch) <= 15 and
        abs(first.last_touch - second.last_touch) <= 15
    )
    if overlap_ratio < 0.40 and not dates_close:
        return False

    indices = np.linspace(
        overlap_start,
        overlap_end,
        min(7, overlap),
    )
    positions = np.clip(np.rint(indices).astype(int), 0, len(df) - 1)
    atr = np.maximum(true_range(df)[positions], 1e-9)
    distances = np.abs(first.y(positions) - second.y(positions)) / atr
    median_distance = float(np.median(distances))
    distance_80 = float(np.quantile(distances, 0.80))
    distance_70 = float(np.quantile(distances, 0.70))
    median_atr = float(np.median(atr))
    slope_divergence = (
        abs(first.slope - second.slope) * max(1, overlap - 1) /
        max(median_atr, 1e-9)
    )
    collinear = (
        overlap_ratio >= 0.40 and
        median_distance <= 0.55 and
        distance_80 <= 0.90 and
        slope_divergence <= 1.25
    )
    nested_visual = (
        overlap_ratio >= 0.85 and
        median_distance <= 1.05 and
        distance_70 <= 1.50 and
        slope_divergence <= 2.00
    )
    close_boundaries = (
        dates_close and
        median_distance <= 0.90 and
        distance_80 <= 1.40 and
        slope_divergence <= 3.00
    )
    return collinear or nested_visual or close_boundaries


def _nms(df: pd.DataFrame, candidates: list[TrendResult], max_results: int,
         score_fn=None) -> list[TrendResult]:
    """Suppress geometrically coincident candidates while preserving fan lines."""
    selected: list[TrendResult] = []
    score_fn = score_fn or (lambda z: z.score)
    for r in sorted(candidates, key=score_fn, reverse=True):
        if not any(_lines_are_duplicates(df, r, kept) for kept in selected):
            selected.append(r)
        if len(selected) >= max_results:
            break
    return selected


def search_trends(df: pd.DataFrame, threshold: float = 75.0,
                  max_results: int = 8) -> list[TrendResult]:
    """Backward-compatible flat output."""
    pool = [r for r in _candidate_pool(df) if r.score >= threshold]
    return _nms(df, pool, max_results=max_results)


def _latest_gap(df: pd.DataFrame, r: TrendResult) -> float:
    i = len(df) - 1
    d = 1 if r.direction == "up" else -1
    arrays = _analysis_arrays(df)
    ref = arrays.anchor(r.direction)[i]
    return float(d * (ref - r.y(i)) / max(arrays.atr[i], 1e-9))


def _post_touch_close_gaps(df: pd.DataFrame, r: TrendResult) -> np.ndarray:
    indices = np.arange(r.last_touch, len(df))
    d = 1 if r.direction == "up" else -1
    arrays = _analysis_arrays(df)
    atr = np.maximum(arrays.atr[r.last_touch:], 1e-9)
    return d * (arrays.close[r.last_touch:] - r.y(indices)) / atr


def _break_confirmation_offset(close_gaps: np.ndarray) -> int | None:
    for index, gap in enumerate(close_gaps):
        if gap < -SEVERE_BREAK_CLOSE_DISTANCE_ATR:
            return index
        if (
            index > 0 and
            close_gaps[index - 1] < -BREAK_CLOSE_DISTANCE_ATR and
            gap < -BREAK_CLOSE_DISTANCE_ATR
        ):
            return index
    return None


def _acceleration_end_offset(close_gaps: np.ndarray) -> int | None:
    """Find a lasting move away from the old line into a faster trend."""
    for index, gap in enumerate(close_gaps):
        future = close_gaps[index + 1:]
        if (
            gap >= ACCELERATION_END_DISTANCE_ATR and
            len(future) >= ACCELERATION_CONFIRM_BARS and
            np.min(future) > ACCELERATION_REENTRY_DISTANCE_ATR
        ):
            return index
    return None


def _confirmed_break_index(
        df: pd.DataFrame,
        r: TrendResult,
) -> int | None:
    offset = _break_confirmation_offset(_post_touch_close_gaps(df, r))
    return None if offset is None else r.last_touch + offset


def _confirmed_acceleration_index(
        df: pd.DataFrame,
        r: TrendResult,
) -> int | None:
    offset = _acceleration_end_offset(_post_touch_close_gaps(df, r))
    return None if offset is None else r.last_touch + offset


def _extend_contact_evidence(
        df: pd.DataFrame,
        r: TrendResult) -> TrendResult:
    """Rescan a fixed line through its valid projection for new evidence."""
    termination_indices = [
        index
        for index in (
            _confirmed_break_index(df, r),
            _confirmed_acceleration_index(df, r),
        )
        if index is not None
    ]
    evidence_end = (
        min(termination_indices) - 1
        if termination_indices
        else len(df) - 1
    )
    evidence_end = max(r.start, evidence_end)
    indices = np.arange(r.start, evidence_end + 1)
    arrays = _analysis_arrays(df)
    d = 1 if r.direction == "up" else -1
    atr = np.maximum(arrays.atr[r.start:evidence_end + 1], 1e-9)
    line = r.y(indices)
    anchor_gap = d * (
        arrays.anchor(r.direction)[r.start:evidence_end + 1] - line
    ) / atr
    body_gap = d * (
        arrays.body_edge(r.direction)[r.start:evidence_end + 1] - line
    ) / atr
    wick_values = (
        df["Low"].to_numpy()[r.start:evidence_end + 1]
        if r.direction == "up"
        else df["High"].to_numpy()[r.start:evidence_end + 1]
    )
    wick_gap = d * (wick_values - line) / atr
    contacts = _event_metrics(anchor_gap, wick_gap, body_gap)
    if not contacts.touches:
        return r
    extended = replace(
        r,
        proximity=contacts.proximity,
        touches=contacts.touches,
        touch_score=contacts.touch_score,
        rejection=contacts.rejection,
        event_span=contacts.event_span,
        touch_distribution=contacts.touch_distribution,
        max_touch_gap=contacts.max_touch_gap,
        interior_touches=contacts.interior_touches,
        first_touch=r.start + contacts.first_touch,
        last_touch=r.start + contacts.last_touch,
        touch_indices=tuple(
            r.start + index
            for index in contacts.touch_indices
        ),
    )
    return replace(extended, score=_long_score_from_metrics(extended))


def _latest_close_gap(df: pd.DataFrame, r: TrendResult) -> float:
    return float(_post_touch_close_gaps(df, r)[-1])


def _previous_close_gap(df: pd.DataFrame, r: TrendResult) -> float:
    index = max(0, len(df) - 2)
    direction = 1 if r.direction == "up" else -1
    arrays = _analysis_arrays(df)
    return float(
        direction
        * (arrays.close[index] - r.y(index))
        / max(arrays.atr[index], 1e-9)
    )


def _post_fit_gaps(df: pd.DataFrame, r: TrendResult) -> np.ndarray:
    """Evaluate an older short line by extrapolating it through today's bar."""
    indices = np.arange(r.end, len(df))
    d = 1 if r.direction == "up" else -1
    arrays = _analysis_arrays(df)
    refs = arrays.anchor(r.direction)[r.end:]
    atr = np.maximum(arrays.atr[r.end:], 1e-9)
    return d * (refs - r.y(indices)) / atr


def _post_touch_gaps(df: pd.DataFrame, r: TrendResult) -> np.ndarray:
    """Evaluate whether a confirmed support/resistance structure still holds."""
    indices = np.arange(r.last_touch, len(df))
    d = 1 if r.direction == "up" else -1
    arrays = _analysis_arrays(df)
    refs = arrays.anchor(r.direction)[r.last_touch:]
    atr = np.maximum(arrays.atr[r.last_touch:], 1e-9)
    return d * (refs - r.y(indices)) / atr


def short_progress_score(r: TrendResult) -> float:
    """Tier-specific score: momentum/efficiency matter more than 3+ rejections."""
    significance = 1.0 / (1.0 + np.exp(-1.4 * (r.drift_t - 0.70)))
    return 100 * (
        0.16 * r.integrity + 0.16 * r.body_integrity +
        0.14 * r.proximity + 0.14 * r.efficiency +
        0.12 * r.slope_strength + 0.08 * significance +
        0.12 * r.touch_score + 0.04 * r.rejection +
        0.02 * r.event_span + 0.02 * r.touch_distribution
    )


def _distribution_penalty_factor(tier: Tier, r: TrendResult) -> float:
    """Smoothly discount endpoint-heavy lines without a pass/fail cliff."""
    quality = float(np.clip(
        r.touch_distribution / TOUCH_DISTRIBUTION_FULL_QUALITY,
        0,
        1,
    ))
    weakness = (1.0 - quality) ** 1.5
    return 1.0 - TOUCH_DISTRIBUTION_PENALTY[tier] * weakness


def _apply_distribution_penalty(
        tier: Tier,
        r: TrendResult,
        score: float,
) -> float:
    return score * _distribution_penalty_factor(tier, r)


def _freshness_extra(age: int) -> float:
    if age <= 45:
        return 0.0
    if age <= 75:
        return 2.0
    if age <= 105:
        return 6.0
    return 8.0


def _is_display_fresh(score: float, age: int) -> bool:
    if age > 105:
        return score >= 80.0
    if age > 75:
        return score >= 78.0
    return True


def _is_recent_medium_reversal(r: TrendResult, n: int) -> bool:
    return (
        r.structure_length <= 30 and
        0 <= n - 1 - r.end <= 12
    )


def _medium_selection_score(r: TrendResult, n: int) -> float:
    score = medium_trend_score(r)
    if _is_recent_medium_reversal(r, n):
        score = max(score, short_progress_score(r))
    return score


def _is_live_structure(df: pd.DataFrame, r: TrendResult) -> bool:
    cached = getattr(r, "_live_structure_cache", None)
    if cached is not None and cached[0] == len(df):
        return cached[1]
    value = (
        np.min(_post_touch_gaps(df, r)) >= -0.50 and
        _latest_gap(df, r) <= 1.25
    )
    r._live_structure_cache = (len(df), bool(value))
    return bool(value)


def _tier_score(
        tier: Tier,
        trend: TrendResult,
        n: int,
        df: pd.DataFrame,
) -> float:
    score, _ = _tier_score_with_formula(tier, trend, n, df)
    return score


def _tier_score_with_formula(
        tier: Tier,
        trend: TrendResult,
        n: int,
        df: pd.DataFrame,
) -> tuple[float, str]:
    if tier == "short":
        score = short_progress_score(trend)
        formula = "short"
    elif tier == "medium":
        medium_score = medium_trend_score(trend)
        short_score = short_progress_score(trend)
        use_short = (
            _is_recent_medium_reversal(trend, n) or
            _is_live_structure(df, trend)
        ) and short_score > medium_score
        score = short_score if use_short else medium_score
        formula = "short" if use_short else "medium"
    else:
        score = trend.score
        formula = "long"
    return _apply_distribution_penalty(tier, trend, score), formula


def _deduplicate_across_tiers(
        df: pd.DataFrame,
        items: dict[Tier, list[TieredTrend]],
) -> dict[Tier, list[TieredTrend]]:
    """Cluster coincident lines and retain one strong, broad representative."""
    flat = [
        item
        for tier in ("long", "medium", "short")
        for item in items[tier]
    ]
    if len(flat) < 2:
        return items

    parents = list(range(len(flat)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = root(first), root(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first in range(len(flat) - 1):
        for second in range(first + 1, len(flat)):
            if _lines_are_duplicates(
                    df, flat[first].trend, flat[second].trend):
                union(first, second)

    clusters: dict[int, list[TieredTrend]] = {}
    for index, item in enumerate(flat):
        clusters.setdefault(root(index), []).append(item)

    representatives = []
    for cluster in clusters.values():
        best_score = max(item.tier_score for item in cluster)
        near_best = [
            item for item in cluster
            if item.tier_score >= best_score - 10.0
        ]
        representative = max(
            near_best,
            key=lambda item: (
                item.trend.structure_length,
                item.trend.last_touch,
                item.tier_score,
            ),
        )
        representatives.append(representative)

    representatives.sort(
        key=lambda item: (
            item.tier_score - _freshness_extra(item.age),
            item.trend.structure_length,
        ),
        reverse=True,
    )
    return {
        tier: [item for item in representatives if item.tier == tier]
        for tier in ("long", "medium", "short")
    }


def _family_primary_rank(
        item: TieredTrend,
        n: int,
) -> tuple[float, ...]:
    trend = item.trend
    quality = (
        item.tier_score +
        6.0 * trend.touch_distribution +
        4.0 * min(1.0, trend.structure_length / max(1, n))
    )
    return (
        1.0 if item.active else 0.0,
        quality,
        item.tier_score,
        trend.touch_distribution,
        trend.structure_length,
        trend.last_touch,
    )


def _trend_family_metrics(
        df: pd.DataFrame,
        candidate: TrendResult,
        primary: TrendResult,
) -> TrendFamilyMetrics:
    overlap_start = max(candidate.first_touch, primary.first_touch)
    overlap_end = min(candidate.last_touch, primary.last_touch)
    overlap = overlap_end - overlap_start + 1
    if overlap <= 1:
        return TrendFamilyMetrics(0.0, float("inf"), float("inf"),
                                  float("inf"), 0, 0)

    overlap_ratio = overlap / min(
        candidate.structure_length,
        primary.structure_length,
    )
    positions = np.arange(overlap_start, overlap_end + 1)
    atr = np.maximum(true_range(df)[positions], 1e-9)
    distances = np.abs(
        candidate.y(positions) - primary.y(positions)
    ) / atr
    primary_slope = max(abs(primary.slope), 1e-9)
    slope_difference = abs(
        candidate.slope - primary.slope
    ) / primary_slope

    novel_touches = 0
    for index in candidate.touch_indices:
        if index < primary.first_touch or index > primary.last_touch:
            novel_touches += 1
            continue
        local_atr = max(float(true_range(df)[index]), 1e-9)
        line_distance = abs(float(
            candidate.y(index) - primary.y(index)
        )) / local_atr
        if line_distance > TREND_FAMILY_NOVEL_TOUCH_DISTANCE_ATR:
            novel_touches += 1

    separation_run = _longest_true_run(
        distances > TREND_FAMILY_SEPARATION_DISTANCE_ATR
    )
    return TrendFamilyMetrics(
        overlap_ratio=float(overlap_ratio),
        median_distance=float(np.median(distances)),
        distance_80=float(np.quantile(distances, 0.80)),
        slope_difference=float(slope_difference),
        novel_touches=novel_touches,
        separation_run=separation_run,
    )


def _is_same_trend_family(metrics: TrendFamilyMetrics) -> bool:
    return (
        metrics.overlap_ratio >= TREND_FAMILY_MIN_OVERLAP and
        metrics.median_distance <= TREND_FAMILY_MAX_MEDIAN_DISTANCE_ATR and
        metrics.distance_80 <= TREND_FAMILY_MAX_DISTANCE_80_ATR
    )


def _is_useful_stage_line(
        candidate: TieredTrend,
        primary: TieredTrend,
        metrics: TrendFamilyMetrics,
) -> bool:
    return (
        candidate.tier_score >= (
            primary.tier_score - TREND_FAMILY_MAX_STAGE_SCORE_GAP
        ) and
        metrics.novel_touches >= 2 and
        metrics.separation_run >= TREND_FAMILY_MIN_SEPARATION_RUN and
        metrics.slope_difference >= TREND_FAMILY_MIN_SLOPE_DIFFERENCE
    )


def _consolidate_trend_families(
        df: pd.DataFrame,
        items: dict[Tier, list[TieredTrend]],
) -> dict[Tier, list[TieredTrend]]:
    flat = [
        item
        for tier in ("long", "medium", "short")
        for item in items[tier]
    ]
    if len(flat) < 2:
        return items

    ranked = sorted(
        flat,
        key=lambda item: _family_primary_rank(item, len(df)),
        reverse=True,
    )
    families: list[list[TieredTrend]] = []
    for item in ranked:
        matches = []
        for family_index, family in enumerate(families):
            primary = family[0]
            if item.trend.direction != primary.trend.direction:
                continue
            metrics = _trend_family_metrics(
                df,
                item.trend,
                primary.trend,
            )
            if _is_same_trend_family(metrics):
                affinity = (
                    metrics.overlap_ratio,
                    -metrics.median_distance,
                    -metrics.distance_80,
                )
                matches.append((affinity, family_index))
        if matches:
            _, family_index = max(matches)
            families[family_index].append(item)
        else:
            families.append([item])

    retained: list[TieredTrend] = []
    for family in families:
        primary = family[0]
        primary.family_id = primary.id
        primary.family_role = (
            "standalone"
            if len(family) == 1
            else "primary"
        )
        retained.append(primary)

        stage_candidates = []
        for candidate in family[1:]:
            metrics = _trend_family_metrics(
                df,
                candidate.trend,
                primary.trend,
            )
            if _is_useful_stage_line(candidate, primary, metrics):
                stage_rank = (
                    metrics.novel_touches,
                    metrics.separation_run,
                    metrics.slope_difference,
                    candidate.tier_score,
                    candidate.trend.structure_length,
                )
                stage_candidates.append((stage_rank, candidate))
        if stage_candidates:
            _, stage = max(stage_candidates, key=lambda value: value[0])
            stage.family_id = primary.id
            stage.family_role = "stage"
            retained.append(stage)

    return {
        tier: [item for item in retained if item.tier == tier]
        for tier in ("long", "medium", "short")
    }


def _limit_display_items(
        items: dict[Tier, list[TieredTrend]],
) -> dict[Tier, list[TieredTrend]]:
    limits = {"long": 3, "medium": 4, "short": 2}
    result: dict[Tier, list[TieredTrend]] = {
        "long": [],
        "medium": [],
        "short": [],
    }
    ranked = sorted(
        (
            item
            for tier in ("long", "medium", "short")
            for item in items[tier]
        ),
        key=lambda item: (
            item.tier_score - _freshness_extra(item.age),
            item.trend.structure_length,
        ),
        reverse=True,
    )
    for item in ranked:
        if sum(len(values) for values in result.values()) >= 6:
            break
        if len(result[item.tier]) < limits[item.tier]:
            result[item.tier].append(item)
    return result


def _is_flat_low_amplitude_noise(df: pd.DataFrame, r: TrendResult) -> bool:
    first = max(0, r.first_touch)
    last = min(len(df) - 1, r.last_touch)
    if last <= first:
        return False

    line_start = float(r.y(first))
    line_end = float(r.y(last))
    line_move = abs(line_end - line_start) / max(abs(line_start), 1e-9)
    median_atr = float(np.median(true_range(df)[first:last + 1]))
    slope_atr_per_20 = (
        abs(r.slope) * 20 / max(median_atr, 1e-9)
    )
    return (
        slope_atr_per_20 <= FLAT_LINE_MAX_SLOPE_ATR_PER_20 and
        line_move <= FLAT_LINE_MAX_ENDPOINT_MOVE_RATIO
    )


def _is_countertrend_bridge_noise(
        item: TieredTrend,
        candidates: list[TieredTrend],
) -> bool:
    r = item.trend
    containers = [
        other
        for other in candidates
        if other is not item
        and other.trend.direction != r.direction
        and other.tier_score > item.tier_score
        and other.trend.first_touch <= r.first_touch
        and other.trend.last_touch >= r.last_touch
    ]
    for first in range(len(containers) - 1):
        for second in range(first + 1, len(containers)):
            if containers[first].trend.direction == containers[second].trend.direction:
                return True
    return False


def _filter_display_noise(
        df: pd.DataFrame,
        items: dict[Tier, list[TieredTrend]],
) -> dict[Tier, list[TieredTrend]]:
    flat = [
        item
        for tier in ("long", "medium", "short")
        for item in items[tier]
    ]
    if not flat:
        return items

    keep = {
        item.id
        for item in flat
        if not _is_flat_low_amplitude_noise(df, item.trend)
        and not _is_countertrend_bridge_noise(item, flat)
    }
    return {
        tier: [item for item in values if item.id in keep]
        for tier, values in items.items()
    }


def search_trend_hierarchy(
        df: pd.DataFrame,
        long_threshold: float = 55.0,
        medium_threshold: float = 70.0,
        short_threshold: float = 64.0,
) -> dict[Tier, list[TieredTrend]]:
    """Build nested long/medium/short output for a 150-candle decision chart.

    Long and medium tiers may include completed historical structures. Every
    unbroken structure projects to today; broken structures project through
    the candle that confirms the break.
    """
    n = len(df)
    short_max, long_min = _tier_boundaries(n)
    pool = _candidate_pool(df)

    long_candidates = [
        r for r in pool
        if r.structure_length >= long_min
        and _tier_score("long", r, n, df) >= (
            long_threshold + _freshness_extra(n - 1 - r.last_touch)
        )
        and _is_display_fresh(
            _tier_score("long", r, n, df),
            n - 1 - r.last_touch,
        )
        and r.touches >= 3 and r.event_span >= 0.38
        and r.drift_t >= 0.55
        and r.max_body_breach_run <= 2
    ]
    medium_candidates = [
        r for r in pool
        if short_max < r.structure_length < long_min
        and _tier_score("medium", r, n, df) >= (
            (66.0 if (
                _is_recent_medium_reversal(r, n) or
                _is_live_structure(df, r)
            ) else medium_threshold) +
            _freshness_extra(n - 1 - r.last_touch)
        )
        and _is_display_fresh(
            _tier_score("medium", r, n, df),
            n - 1 - r.last_touch,
        )
        and r.touches >= 2
        and r.event_span >= 0.28 and r.drift_t >= 0.65
        and (
            r.touches >= 3 or
            _tier_score("medium", r, n, df) >= (
                (66.0 if (
                    _is_recent_medium_reversal(r, n) or
                    _is_live_structure(df, r)
                ) else medium_threshold) +
                _freshness_extra(n - 1 - r.last_touch) + 4
            )
        )
        and r.max_body_breach_run <= 2
    ]
    short_candidates = [
        r for r in pool
        if 7 <= r.structure_length <= short_max
        and 0 <= n - 1 - r.end <= 9
        and _tier_score("short", r, n, df) >= short_threshold
        and r.touches >= 2
        and r.event_span >= 0.28 and r.drift_t >= 0.85
        and (
            r.structure_length >= 10 or
            _tier_score("short", r, n, df) >= short_threshold + 7
        )
        and (
            r.touches >= 3 or
            _tier_score("short", r, n, df) >= short_threshold + 3
        )
        and r.max_body_breach_run <= 2
    ]

    chosen = {
        "long": _nms(
            df,
            long_candidates,
            max_results=6,
            score_fn=lambda trend: _tier_score("long", trend, n, df),
        ),
        "medium": _nms(
            df,
            medium_candidates,
            max_results=8,
            score_fn=lambda trend: _tier_score("medium", trend, n, df),
        ),
        "short": _nms(
            df,
            short_candidates,
            max_results=4,
            score_fn=lambda trend: _tier_score("short", trend, n, df),
        ),
    }
    chosen = {
        tier: [
            _extend_contact_evidence(df, trend)
            for trend in trends
        ]
        for tier, trends in chosen.items()
    }
    items: dict[Tier, list[TieredTrend]] = {"long": [], "medium": [], "short": []}
    for tier, values in chosen.items():
        for r in values:
            age = n - 1 - r.last_touch
            current_gap = _latest_close_gap(df, r)
            previous_gap = _previous_close_gap(df, r)
            break_index = _confirmed_break_index(df, r)
            acceleration_index = _confirmed_acceleration_index(df, r)
            termination_indices = [
                index
                for index in (break_index, acceleration_index)
                if index is not None
            ]
            active = not termination_indices
            if termination_indices:
                status = "broken"
            elif current_gap <= CHALLENGE_DISTANCE_ATR:
                status = "challenging"
            else:
                status = "trending"
            tier_score, score_formula = _tier_score_with_formula(
                tier,
                r,
                n,
                df,
            )
            items[tier].append(TieredTrend(
                tier=tier,
                trend=r,
                active=active,
                status=status,
                age=age,
                current_gap=current_gap,
                previous_gap=previous_gap,
                tier_score=tier_score,
                break_index=break_index,
                acceleration_index=acceleration_index,
                score_formula=score_formula,
            ))

    items = _deduplicate_across_tiers(df, items)
    items = _filter_display_noise(df, items)
    items = _consolidate_trend_families(df, items)
    items = _limit_display_items(items)

    # Attach each child to the smallest same-direction parent containing at
    # least 80% of the child. Orphans remain valid standalone trends.
    parents = items["long"] + items["medium"]
    for tier in ("medium", "short"):
        for child in items[tier]:
            eligible = []
            for parent in parents:
                if parent.tier == child.tier or parent.trend.direction != child.trend.direction:
                    continue
                inter = max(
                    0,
                    min(child.trend.last_touch, parent.trend.last_touch) -
                    max(child.trend.first_touch, parent.trend.first_touch) + 1,
                )
                if (
                    inter / child.trend.structure_length >= 0.80 and
                    parent.trend.structure_length > child.trend.structure_length
                ):
                    eligible.append(parent)
            if eligible:
                child.parent_id = min(
                    eligible,
                    key=lambda x: x.trend.structure_length,
                ).id
    return items


def _window_fingerprint(candles: list[dict]) -> str:
    serialized = json.dumps(
        candles,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(serialized, digest_size=16).hexdigest()


def _cached_analysis_result(key: tuple) -> list[dict] | None:
    with _analysis_result_cache_lock:
        cached = _analysis_result_cache.get(key)
        if cached is None:
            return None
        _analysis_result_cache.move_to_end(key)
        return [dict(item) for item in cached]


def _store_analysis_result(key: tuple, trends: list[dict]) -> None:
    immutable_copy = tuple(dict(item) for item in trends)
    with _analysis_result_cache_lock:
        _analysis_result_cache[key] = immutable_copy
        _analysis_result_cache.move_to_end(key)
        while len(_analysis_result_cache) > ANALYSIS_CACHE_MAX_SIZE:
            _analysis_result_cache.popitem(last=False)


def clear_trendline_analysis_cache() -> None:
    """Clear memoized API results, primarily for tests and maintenance."""
    with _analysis_result_cache_lock:
        _analysis_result_cache.clear()


def _prepare_trendline_analysis(
        symbol: str,
        period: str,
        limit: int,
    show_weekend_data: str | bool | None,
    adjustment: str = "all",
) -> dict:
    clean_period = (period or "1D").upper()
    if clean_period not in SUPPORTED_PERIODS:
        raise ValueError("Unsupported analysis period")

    window_size = int(limit or 150)
    if window_size < 30 or window_size > 300:
        raise ValueError("Analysis window must be between 30 and 300 candles")

    payload = get_market_data(symbol, adjustment=adjustment)
    raw_rows = payload.get("data") or []
    include_weekends = resolve_show_weekend_data(
        show_weekend_data,
        payload.get("symbol_settings") or {},
    )
    if not include_weekends:
        raw_rows = [row for row in raw_rows if not is_weekend_date(row["date"])]
    candles = aggregate_rows(raw_rows, clean_period)
    window_start = max(0, len(candles) - window_size)
    window = candles[window_start:]
    return {
        "market_payload": payload,
        "period": clean_period,
        "requested_window_size": window_size,
        "show_weekend_data": include_weekends,
        "candles": candles,
        "window_start": window_start,
        "window": window,
        "data_fingerprint": _window_fingerprint(window),
    }


def get_trendline_analysis_signature(
        symbol: str,
        period: str = "1D",
        limit: int = 150,
    show_weekend_data: str | bool | None = None,
    adjustment: str = "all",
) -> dict:
    prepared = _prepare_trendline_analysis(
        symbol,
        period,
        limit,
        show_weekend_data,
        adjustment,
    )
    payload = prepared["market_payload"]
    candles = prepared["candles"]
    return {
        "symbol": payload.get("symbol") or symbol,
        "canonical_symbol": (
            payload.get("canonical_symbol")
            or payload.get("symbol")
            or symbol
        ),
        "period": prepared["period"],
        "requested_window_size": prepared["requested_window_size"],
        "show_weekend_data": prepared["show_weekend_data"],
        "data_count": len(candles),
        "latest_data_date": candles[-1]["date"] if candles else None,
        "data_fingerprint": prepared["data_fingerprint"],
    }


def analyze_symbol_trendlines(symbol: str, period: str = "1D",
                              limit: int = 150,
                              show_weekend_data: str | bool | None = None,
                              adjustment: str = "all") -> dict:
    """Analyze the latest candles for one symbol and return drawable lines."""
    prepared = _prepare_trendline_analysis(
        symbol,
        period,
        limit,
        show_weekend_data,
        adjustment,
    )
    payload = prepared["market_payload"]
    clean_period = prepared["period"]
    window_size = prepared["requested_window_size"]
    include_weekends = prepared["show_weekend_data"]
    candles = prepared["candles"]
    window_start = prepared["window_start"]
    window = prepared["window"]
    data_fingerprint = prepared["data_fingerprint"]
    if len(candles) < 7:
        return {
            "ok": True,
            "symbol": payload.get("symbol") or symbol,
            "canonical_symbol": payload.get("canonical_symbol") or symbol,
            "period": clean_period,
            "requested_window_size": window_size,
            "show_weekend_data": include_weekends,
            "window_start_index": 0,
            "window_size": len(candles),
            "data_count": len(candles),
            "latest_data_date": candles[-1]["date"] if candles else None,
            "data_fingerprint": data_fingerprint,
            "trends": [],
            "message": "K线数量不足，无法识别趋势线。",
        }

    cache_key = (
        ANALYSIS_CACHE_VERSION,
        payload.get("canonical_symbol") or payload.get("symbol") or symbol,
        clean_period,
        window_size,
        include_weekends,
        window_start,
        len(candles),
        data_fingerprint,
    )
    trends = _cached_analysis_result(cache_key)
    if trends is None:
        df = candles_to_dataframe(window)
        hierarchy = search_trend_hierarchy(df)
        trends = serialize_hierarchy(
            hierarchy,
            window_start,
            len(candles) - 1,
        )
        _attach_trend_dates(trends, candles)
        _store_analysis_result(cache_key, trends)

    return {
        "ok": True,
        "symbol": payload.get("symbol") or symbol,
        "canonical_symbol": payload.get("canonical_symbol") or symbol,
        "source": payload.get("source"),
        "show_weekend_data": include_weekends,
        "period": clean_period,
        "requested_window_size": window_size,
        "window_start_index": window_start,
        "window_size": len(window),
        "data_count": len(candles),
        "latest_data_date": candles[-1]["date"],
        "data_fingerprint": data_fingerprint,
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
    start_index = window_start + trend.first_touch
    end_index = window_start + trend.last_touch
    required_touches = 3 if item.tier == "long" else 2
    formation_touch_offset = min(
        required_touches - 1,
        len(trend.touch_indices) - 1,
    )
    formation_end_local = trend.touch_indices[formation_touch_offset]
    formation_end_index = window_start + formation_end_local
    break_index = (
        None
        if item.break_index is None
        else window_start + item.break_index
    )
    acceleration_index = (
        None
        if item.acceleration_index is None
        else window_start + item.acceleration_index
    )
    termination_indices = [
        index
        for index in (break_index, acceleration_index)
        if index is not None
    ]
    termination_index = min(termination_indices, default=None)
    end_reason = None
    termination_confirmed_index = None
    if termination_index is not None:
        end_reason = (
            "break"
            if termination_index == break_index
            else "acceleration"
        )
        termination_confirmed_index = (
            break_index
            if end_reason == "break"
            else min(
                latest_index,
                acceleration_index + ACCELERATION_CONFIRM_BARS,
            )
        )
    projection_end_index = (
        latest_index
        if item.active
        else max(end_index, termination_index or end_index)
    )
    local_projection_end = projection_end_index - window_start
    start_price = float(trend.y(trend.first_touch))
    end_price = float(trend.y(trend.last_touch))
    projection_end_price = float(trend.y(local_projection_end))
    return {
        "id": item.id,
        "tier": item.tier,
        "direction": trend.direction,
        "start_index": start_index,
        "formation_end_index": formation_end_index,
        "end_index": end_index,
        "projection_end_index": projection_end_index,
        "break_index": break_index,
        "acceleration_index": acceleration_index,
        "termination_index": termination_index,
        "termination_confirmed_index": termination_confirmed_index,
        "end_reason": end_reason,
        "start_price": start_price,
        "end_price": end_price,
        "projection_end_price": projection_end_price,
        "slope": float(trend.slope),
        "intercept": float(trend.intercept),
        "score": round(float(trend.score), 4),
        "tier_score": round(float(item.tier_score), 4),
        "score_formula": item.score_formula or item.tier,
        "integrity": round(float(trend.integrity), 4),
        "proximity": round(float(trend.proximity), 4),
        "touches": int(trend.touches),
        "touch_score": round(float(trend.touch_score), 4),
        "rejection": round(float(trend.rejection), 4),
        "event_span": round(float(trend.event_span), 4),
        "touch_distribution": round(float(trend.touch_distribution), 4),
        "max_touch_gap": round(float(trend.max_touch_gap), 4),
        "interior_touches": int(trend.interior_touches),
        "touch_indices": [
            window_start + index
            for index in trend.touch_indices
        ],
        "fit_start_index": window_start + trend.start,
        "fit_end_index": window_start + trend.end,
        "body_integrity": round(float(trend.body_integrity), 4),
        "body_breach_ratio": round(float(trend.body_breach_ratio), 4),
        "severe_body_breach_ratio": round(
            float(trend.severe_body_breach_ratio), 4
        ),
        "max_body_breach_run": int(trend.max_body_breach_run),
        "efficiency": round(float(trend.efficiency), 4),
        "slope_strength": round(float(trend.slope_strength), 4),
        "drift_t": round(float(trend.drift_t), 4),
        "active": bool(item.active),
        "parent_id": item.parent_id,
        "family_id": item.family_id,
        "family_role": item.family_role,
        "status": item.status,
        "age": int(item.age),
        "current_gap": round(float(item.current_gap), 4),
        "current_close_gap": round(float(item.current_gap), 4),
        "previous_close_gap": round(float(item.previous_gap), 4),
        "distribution_penalty_factor": round(
            float(_distribution_penalty_factor(item.tier, trend)),
            4,
        ),
    }


def _attach_trend_dates(trends: list[dict], candles: list[dict]) -> None:
    date_fields = {
        "start_index": "start_date",
        "formation_end_index": "formation_date",
        "end_index": "last_touch_date",
        "projection_end_index": "projection_end_date",
        "termination_index": "termination_date",
        "termination_confirmed_index": "termination_confirmed_date",
    }
    for trend in trends:
        for index_field, date_field in date_fields.items():
            index = trend.get(index_field)
            trend[date_field] = (
                candles[index]["date"]
                if isinstance(index, int) and 0 <= index < len(candles)
                else None
            )


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
