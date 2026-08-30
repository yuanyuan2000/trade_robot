"""Causal horizontal support/resistance zone analysis for OHLC candles.

Only confirmed pivots participate in a zone.  A pivot at index ``i`` becomes
available at ``i + pivot_right_bars``; no function in this module reads bars
after the analysis window.  This makes the same core safe to call on a
walk-forward prefix during research or backtesting.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
from typing import Literal

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

from services.trendline_analysis_service import (
    SUPPORTED_PERIODS,
    _prepare_trendline_analysis,
    candles_to_dataframe,
)


ZoneRole = Literal["support", "resistance"]
PivotType = Literal["high", "low"]

KEY_ZONE_ALGORITHM_VERSION = "key-zone-dbscan-v8-confirmation-rejection"
KEY_ZONE_CACHE_MAX_SIZE = 64
SCORE_WEIGHTS = {
    "tests": 0.25,
    "prominence": 0.15,
    "rejection": 0.20,
    "span": 0.15,
    "recency": 0.10,
    "integrity": 0.15,
}


@dataclass(frozen=True)
class KeyZoneConfig:
    atr_period: int = 14
    pivot_left_bars: int = 5
    pivot_right_bars: int = 3
    min_pivot_distance: int = 4
    prominence_threshold_atr: float = 0.50
    min_independent_test_gap: int = 5
    time_half_life: float = 75.0
    base_test_score: float = 1.0
    prominence_weight: float = 0.50
    rejection_weight: float = 0.75
    prominence_cap_atr: float = 5.0
    rejection_cap_atr: float = 5.0
    dbscan_eps_atr: float = 0.75
    dbscan_min_samples: int = 2
    min_zone_tests: int = 2
    min_zone_halfwidth_atr: float = 0.15
    max_zone_halfwidth_atr: float = 0.60
    challenge_distance_atr: float = 0.50
    break_distance_atr: float = 0.30
    severe_break_distance_atr: float = 0.80
    acceptance_distance_atr: float = 4.0
    shoulder_extreme_tolerance_atr: float = 0.25
    shoulder_price_distance_atr: float = 0.50
    shoulder_min_prominence_atr: float = 0.40
    shoulder_min_rejection_atr: float = 0.75
    validation_distance_atr: float = 0.50
    validation_min_rejection_atr: float = 0.75
    edge_confirmation_right_bars: int = 2
    integrity_score_floor: float = 0.50

    def validate(self) -> None:
        integer_values = {
            "atr_period": self.atr_period,
            "pivot_left_bars": self.pivot_left_bars,
            "pivot_right_bars": self.pivot_right_bars,
            "min_pivot_distance": self.min_pivot_distance,
            "dbscan_min_samples": self.dbscan_min_samples,
            "min_zone_tests": self.min_zone_tests,
            "edge_confirmation_right_bars": self.edge_confirmation_right_bars,
        }
        if any(value <= 0 for value in integer_values.values()):
            raise ValueError("Key-zone integer settings must be positive")
        if self.min_independent_test_gap < 0:
            raise ValueError("Independent-test gap cannot be negative")
        if self.time_half_life <= 0 or self.dbscan_eps_atr <= 0:
            raise ValueError("Time half-life and DBSCAN distance must be positive")
        if not 0 < self.min_zone_halfwidth_atr <= self.max_zone_halfwidth_atr:
            raise ValueError("Invalid key-zone width range")
        if not 0 < self.break_distance_atr < self.severe_break_distance_atr:
            raise ValueError("Invalid key-zone break thresholds")
        if self.acceptance_distance_atr <= self.severe_break_distance_atr:
            raise ValueError("Invalid key-zone acceptance threshold")
        if min(
            self.shoulder_extreme_tolerance_atr,
            self.shoulder_price_distance_atr,
            self.shoulder_min_prominence_atr,
            self.shoulder_min_rejection_atr,
            self.validation_distance_atr,
            self.validation_min_rejection_atr,
        ) < 0:
            raise ValueError("Invalid key-zone shoulder or validation threshold")
        if self.edge_confirmation_right_bars >= self.pivot_right_bars:
            raise ValueError("Edge confirmation must be shorter than pivot confirmation")
        if not 0 <= self.integrity_score_floor <= 1:
            raise ValueError("Integrity score floor must be between zero and one")

    def signature(self) -> str:
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_result_cache: OrderedDict[tuple, dict] = OrderedDict()
_result_cache_lock = threading.Lock()


def clear_key_zone_analysis_cache() -> None:
    with _result_cache_lock:
        _result_cache.clear()


def _cached_result(key: tuple) -> dict | None:
    with _result_cache_lock:
        value = _result_cache.get(key)
        if value is None:
            return None
        _result_cache.move_to_end(key)
        return deepcopy(value)


def _store_result(key: tuple, value: dict) -> None:
    with _result_cache_lock:
        _result_cache[key] = deepcopy(value)
        _result_cache.move_to_end(key)
        while len(_result_cache) > KEY_ZONE_CACHE_MAX_SIZE:
            _result_cache.popitem(last=False)


def _normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("K-line data must be a pandas DataFrame")
    columns = {str(column).lower(): column for column in df.columns}
    required = ("open", "high", "low", "close")
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}")

    normalized = pd.DataFrame(index=df.index)
    for name in required:
        normalized[name] = pd.to_numeric(df[columns[name]], errors="coerce")
    if "volume" in columns:
        normalized["volume"] = pd.to_numeric(
            df[columns["volume"]], errors="coerce",
        ).fillna(0.0)
    normalized = normalized.dropna(subset=list(required)).copy()
    values = normalized[list(required)].to_numpy(float)
    if not np.isfinite(values).all():
        raise ValueError("OHLC data contains non-finite values")
    row_scale = normalized[list(required)].abs().max(axis=1).clip(lower=1.0)
    tolerance = row_scale * 1e-8
    invalid = normalized["high"] + tolerance < normalized["low"]
    if bool(invalid.any()):
        raise ValueError("OHLC data contains invalid high/low ranges")
    # Some providers publish a provisional daily open just outside the
    # reported high/low.  Preserve the candle instead of dropping it while
    # enforcing the OHLC envelope used by ATR and pivot calculations.
    normalized["high"] = normalized[["open", "high", "close"]].max(axis=1)
    normalized["low"] = normalized[["open", "low", "close"]].min(axis=1)
    if len(normalized) < 30:
        raise ValueError("At least 30 valid candles are required for key-zone analysis")
    return normalized.reset_index(drop=True)


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=1,
    ).mean()
    atr = atr.replace(0, np.nan).ffill().bfill()
    if atr.isna().any() or bool((atr <= 0).any()):
        raise ValueError("Unable to calculate a positive ATR")
    return atr


def _local_prominence(
    values: np.ndarray,
    index: int,
    left_bars: int,
    right_bars: int,
    pivot_type: PivotType,
) -> float:
    left = values[index - left_bars:index]
    right = values[index + 1:index + right_bars + 1]
    price = float(values[index])
    if pivot_type == "high":
        return max(0.0, min(price - float(left.min()), price - float(right.min())))
    return max(0.0, min(float(left.max()) - price, float(right.max()) - price))


def detect_confirmed_pivots(
    candles: pd.DataFrame,
    config: KeyZoneConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return normalized candles and pivots available by the window end.

    The candidate at ``pivot_index`` is inspected only when
    ``confirmed_at_index = pivot_index + pivot_right_bars`` exists.
    """

    cfg = config or KeyZoneConfig()
    cfg.validate()
    df = _normalize_ohlc(candles)
    df["atr"] = wilder_atr(df, cfg.atr_period)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    n = len(df)
    records: list[dict] = []
    last_accepted: dict[PivotType, int] = {"high": -10_000, "low": -10_000}

    for index in range(cfg.pivot_left_bars, n - cfg.pivot_right_bars):
        high_window = high[
            index - cfg.pivot_left_bars:index + cfg.pivot_right_bars + 1
        ]
        low_window = low[
            index - cfg.pivot_left_bars:index + cfg.pivot_right_bars + 1
        ]
        candidates: list[PivotType] = []
        if int(np.argmax(high_window)) == cfg.pivot_left_bars:
            candidates.append("high")
        if int(np.argmin(low_window)) == cfg.pivot_left_bars:
            candidates.append("low")

        for pivot_type in candidates:
            if index - last_accepted[pivot_type] < cfg.min_pivot_distance:
                continue
            price = float(high[index] if pivot_type == "high" else low[index])
            prominence = _local_prominence(
                high if pivot_type == "high" else low,
                index,
                cfg.pivot_left_bars,
                cfg.pivot_right_bars,
                pivot_type,
            )
            prominence_atr = prominence / atr[index]
            if prominence_atr < cfg.prominence_threshold_atr:
                continue
            right = df.iloc[index + 1:index + cfg.pivot_right_bars + 1]
            if pivot_type == "high":
                rejection = price - float(right["low"].min())
            else:
                rejection = float(right["high"].max()) - price
            rejection_atr = max(0.0, rejection / atr[index])
            confirmed_at = index + cfg.pivot_right_bars
            age = n - 1 - confirmed_at
            time_weight = 2.0 ** (-age / cfg.time_half_life)
            quality = (
                cfg.base_test_score
                + cfg.prominence_weight * min(prominence_atr, cfg.prominence_cap_atr)
                + cfg.rejection_weight * min(rejection_atr, cfg.rejection_cap_atr)
            )
            records.append(
                {
                    "pivot_index": index,
                    "confirmed_at_index": confirmed_at,
                    "type": pivot_type,
                    "price": price,
                    "atr": float(atr[index]),
                    "prominence_atr": float(prominence_atr),
                    "rejection_atr": float(rejection_atr),
                    "age": age,
                    "time_weight": float(time_weight),
                    "test_score": float(time_weight * quality),
                }
            )
            last_accepted[pivot_type] = index

    columns = [
        "pivot_index",
        "confirmed_at_index",
        "type",
        "price",
        "atr",
        "prominence_atr",
        "rejection_atr",
        "age",
        "time_weight",
        "test_score",
    ]
    pivots = pd.DataFrame(records, columns=columns)
    return df, pivots


def _dbscan_labels(pivots: pd.DataFrame, cfg: KeyZoneConfig) -> np.ndarray:
    if pivots.empty:
        return np.empty(0, dtype=int)
    prices = pivots["price"].to_numpy(float)
    atr = pivots["atr"].to_numpy(float)
    difference = np.abs(prices[:, None] - prices[None, :])
    average_atr = (atr[:, None] + atr[None, :]) / 2.0
    distance = np.divide(
        difference,
        average_atr,
        out=np.full_like(difference, np.inf),
        where=average_atr > 0,
    )
    np.fill_diagonal(distance, 0.0)
    return DBSCAN(
        eps=cfg.dbscan_eps_atr,
        min_samples=cfg.dbscan_min_samples,
        metric="precomputed",
    ).fit_predict(distance)


def _independent_tests(group: pd.DataFrame, min_gap: int) -> pd.DataFrame:
    if group.empty or min_gap <= 0:
        return group.sort_values("pivot_index").copy()
    ordered = group.sort_values("pivot_index")
    selected: list[int] = []
    bucket: list[int] = []
    bucket_start: int | None = None
    for row_index, row in ordered.iterrows():
        pivot_index = int(row["pivot_index"])
        if bucket_start is None or pivot_index - bucket_start <= min_gap:
            bucket_start = pivot_index if bucket_start is None else bucket_start
            bucket.append(row_index)
            continue
        selected.append(ordered.loc[bucket, "test_score"].idxmax())
        bucket = [row_index]
        bucket_start = pivot_index
    if bucket:
        selected.append(ordered.loc[bucket, "test_score"].idxmax())
    return ordered.loc[selected].sort_values("pivot_index").copy()


def _shoulder_tests_for_anchor(
    df: pd.DataFrame,
    anchor: pd.Series,
    strict_pivots: pd.DataFrame,
    cfg: KeyZoneConfig,
) -> pd.DataFrame:
    """Find confirmed same-level shoulders for one isolated strict pivot."""
    pivot_type: PivotType = anchor["type"]
    values = df[pivot_type].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    anchor_index = int(anchor["pivot_index"])
    anchor_price = float(anchor["price"])
    anchor_atr = float(anchor["atr"])
    strict_indices = set(
        strict_pivots.loc[
            strict_pivots["type"] == pivot_type,
            "pivot_index",
        ].astype(int)
    )
    records: list[dict] = []
    start = anchor_index + cfg.min_independent_test_gap + 1
    stop = len(df) - cfg.pivot_right_bars
    for index in range(max(cfg.pivot_left_bars, start), stop):
        if index in strict_indices:
            continue
        window = values[
            index - cfg.pivot_left_bars:index + cfg.pivot_right_bars + 1
        ]
        price = float(values[index])
        extreme_gap = (
            float(window.max()) - price
            if pivot_type == "high"
            else price - float(window.min())
        ) / atr[index]
        if extreme_gap > cfg.shoulder_extreme_tolerance_atr:
            continue
        prominence_atr = _local_prominence(
            values,
            index,
            cfg.pivot_left_bars,
            cfg.pivot_right_bars,
            pivot_type,
        ) / atr[index]
        if prominence_atr < cfg.shoulder_min_prominence_atr:
            continue
        right = df.iloc[index + 1:index + cfg.pivot_right_bars + 1]
        rejection = (
            price - float(right["low"].min())
            if pivot_type == "high"
            else float(right["high"].max()) - price
        )
        rejection_atr = max(0.0, rejection / atr[index])
        if rejection_atr < cfg.shoulder_min_rejection_atr:
            continue
        average_atr = (anchor_atr + float(atr[index])) / 2.0
        if abs(price - anchor_price) / average_atr > cfg.shoulder_price_distance_atr:
            continue
        confirmed_at = index + cfg.pivot_right_bars
        age = len(df) - 1 - confirmed_at
        time_weight = 2.0 ** (-age / cfg.time_half_life)
        quality = (
            cfg.base_test_score
            + cfg.prominence_weight
            * min(prominence_atr, cfg.prominence_cap_atr)
            + cfg.rejection_weight
            * min(rejection_atr, cfg.rejection_cap_atr)
        )
        records.append({
            "pivot_index": index,
            "confirmed_at_index": confirmed_at,
            "type": pivot_type,
            "price": price,
            "atr": float(atr[index]),
            "prominence_atr": float(prominence_atr),
            "rejection_atr": float(rejection_atr),
            "age": age,
            "time_weight": float(time_weight),
            "test_score": float(time_weight * quality),
            "evidence_source": "shoulder",
        })
    return pd.DataFrame(records)


def _provisional_edge_pivots(
    df: pd.DataFrame,
    strict_pivots: pd.DataFrame,
    cfg: KeyZoneConfig,
) -> pd.DataFrame:
    """Evaluate only the third-last bar with two right-side bars available."""
    index = len(df) - 1 - cfg.edge_confirmation_right_bars
    if index < cfg.pivot_left_bars:
        return pd.DataFrame()
    atr = df["atr"].to_numpy(float)
    last_accepted = {
        pivot_type: int(
            strict_pivots.loc[
                strict_pivots["type"] == pivot_type,
                "pivot_index",
            ].max()
        )
        if bool((strict_pivots["type"] == pivot_type).any())
        else -10_000
        for pivot_type in ("high", "low")
    }
    records: list[dict] = []
    for pivot_type in ("high", "low"):
        if index - last_accepted[pivot_type] < cfg.min_pivot_distance:
            continue
        values = df[pivot_type].to_numpy(float)
        window = values[
            index - cfg.pivot_left_bars:index + cfg.edge_confirmation_right_bars + 1
        ]
        candidate_offset = cfg.pivot_left_bars
        is_extreme = (
            int(np.argmax(window)) == candidate_offset
            if pivot_type == "high"
            else int(np.argmin(window)) == candidate_offset
        )
        if not is_extreme:
            continue
        price = float(values[index])
        prominence_atr = _local_prominence(
            values,
            index,
            cfg.pivot_left_bars,
            cfg.edge_confirmation_right_bars,
            pivot_type,
        ) / atr[index]
        if prominence_atr < cfg.prominence_threshold_atr:
            continue
        right = df.iloc[index + 1:]
        rejection = (
            price - float(right["low"].min())
            if pivot_type == "high"
            else float(right["high"].max()) - price
        )
        rejection_atr = max(0.0, rejection / atr[index])
        quality = (
            cfg.base_test_score
            + cfg.prominence_weight
            * min(prominence_atr, cfg.prominence_cap_atr)
            + cfg.rejection_weight
            * min(rejection_atr, cfg.rejection_cap_atr)
        )
        records.append({
            "pivot_index": index,
            "confirmed_at_index": len(df) - 1,
            "type": pivot_type,
            "price": price,
            "atr": float(atr[index]),
            "prominence_atr": float(prominence_atr),
            "rejection_atr": float(rejection_atr),
            "age": 0,
            "time_weight": 1.0,
            "test_score": float(quality),
            "evidence_source": "provisional_edge",
        })
    return pd.DataFrame(records)


def _zone_validation_events(
    df: pd.DataFrame,
    start: int,
    role: ZoneRole,
    zone_low: float,
    zone_high: float,
    cfg: KeyZoneConfig,
    stop: int | None = None,
) -> list[dict]:
    """Return causal close-confirmed rejections of an already formed zone."""
    candidates: list[dict] = []
    end = len(df) if stop is None else min(len(df), max(0, stop))
    for index in range(max(0, start), end):
        row = df.iloc[index]
        atr = float(row["atr"])
        high = float(row["high"] if "high" in row.index else row["close"])
        low = float(row["low"] if "low" in row.index else row["close"])
        if role == "resistance":
            approach_gap = (zone_low - high) / atr
            rejection_atr = (high - float(row["close"])) / atr
            correct_close = float(row["close"]) <= zone_low
        else:
            approach_gap = (low - zone_high) / atr
            rejection_atr = (float(row["close"]) - low) / atr
            correct_close = float(row["close"]) >= zone_high
        if (
            approach_gap <= cfg.validation_distance_atr
            and correct_close
            and rejection_atr >= cfg.validation_min_rejection_atr
        ):
            candidates.append({
                "index": index,
                "role": role,
                "approach_gap_atr": float(approach_gap),
                "rejection_atr": float(rejection_atr),
            })
    if not candidates:
        return []
    events: list[dict] = []
    bucket: list[dict] = []
    bucket_start: int | None = None
    for candidate in candidates:
        index = int(candidate["index"])
        if bucket_start is None or index - bucket_start <= cfg.min_independent_test_gap:
            bucket_start = index if bucket_start is None else bucket_start
            bucket.append(candidate)
            continue
        events.append(max(bucket, key=lambda item: (item["rejection_atr"], item["index"])))
        bucket = [candidate]
        bucket_start = index
    if bucket:
        events.append(max(bucket, key=lambda item: (item["rejection_atr"], item["index"])))
    return events


def _weighted_std(values: np.ndarray, weights: np.ndarray, center: float) -> float:
    if len(values) <= 1:
        return 0.0
    weight_sum = float(weights.sum())
    if weight_sum <= 0:
        return float(np.std(values))
    variance = float(np.sum(weights * (values - center) ** 2) / weight_sum)
    return math.sqrt(max(0.0, variance))


def _break_confirmation_index(
    df: pd.DataFrame,
    start: int,
    role: ZoneRole,
    zone_low: float,
    zone_high: float,
    cfg: KeyZoneConfig,
) -> int | None:
    consecutive = 0
    for index in range(max(0, start), len(df)):
        close = float(df["close"].iloc[index])
        atr = float(df["atr"].iloc[index])
        if role == "support":
            gap = (close - zone_low) / atr
        else:
            gap = (zone_high - close) / atr
        if gap <= -cfg.severe_break_distance_atr:
            return index
        if gap <= -cfg.break_distance_atr:
            consecutive += 1
            if consecutive >= 2:
                return index
        else:
            consecutive = 0
    return None


def _zone_state(
    df: pd.DataFrame,
    tests: pd.DataFrame,
    zone_low: float,
    zone_high: float,
    cfg: KeyZoneConfig,
) -> dict:
    formation_row = tests.iloc[cfg.min_zone_tests - 1]
    formation_index = int(formation_row["confirmed_at_index"])
    role: ZoneRole = "resistance" if formation_row["type"] == "high" else "support"
    activation_index = formation_index
    reversal_count = 0
    last_break_index: int | None = None
    pending_retest = False
    validation_events: dict[int, dict] = {}

    while activation_index < len(df):
        expected_type = "low" if role == "support" else "high"
        retest_index: int | None = None
        acceptance_index: int | None = None
        if pending_retest:
            retests = tests[
                (tests["confirmed_at_index"] > activation_index)
                & (tests["type"] == expected_type)
            ]
            if not retests.empty:
                retest_index = int(retests.iloc[0]["confirmed_at_index"])
            for index in range(activation_index + 1, len(df)):
                close = float(df["close"].iloc[index])
                atr = float(df["atr"].iloc[index])
                gap = (
                    (close - zone_high) / atr
                    if role == "support"
                    else (zone_low - close) / atr
                )
                if gap >= cfg.acceptance_distance_atr:
                    acceptance_index = index
                    break

        break_index = _break_confirmation_index(
            df,
            activation_index + 1,
            role,
            zone_low,
            zone_high,
            cfg,
        )
        interval_validations = _zone_validation_events(
            df,
            activation_index + 1,
            role,
            zone_low,
            zone_high,
            cfg,
            break_index,
        )
        validation_index = (
            int(interval_validations[0]["index"])
            if interval_validations
            else None
        )
        pending_confirmations = [
            index
            for index in (retest_index, acceptance_index, validation_index)
            if index is not None
        ]
        confirmation_index = min(pending_confirmations, default=None)
        if pending_retest and confirmation_index is not None and (
            break_index is None or confirmation_index < break_index
        ):
            for event in interval_validations:
                if int(event["index"]) <= confirmation_index:
                    validation_events[int(event["index"])] = event
            pending_retest = False
            activation_index = confirmation_index
            continue

        if break_index is None:
            for event in interval_validations:
                validation_events[int(event["index"])] = event
            current_close = float(df["close"].iloc[-1])
            current_atr = float(df["atr"].iloc[-1])
            if role == "support":
                current_gap = (current_close - zone_high) / current_atr
            else:
                current_gap = (zone_low - current_close) / current_atr
            latest = df.iloc[-1]
            latest_high = float(
                latest["high"] if "high" in latest.index else latest["close"]
            )
            latest_low = float(
                latest["low"] if "low" in latest.index else latest["close"]
            )
            if role == "support":
                current_extreme_gap = (
                    latest_low - zone_high
                ) / current_atr
            else:
                current_extreme_gap = (
                    zone_low - latest_high
                ) / current_atr
            latest_validation = (
                (len(df) - 1) in validation_events
                or bool(_zone_validation_events(
                    df,
                    len(df) - 1,
                    role,
                    zone_low,
                    zone_high,
                    cfg,
                ))
            )
            status = (
                "challenging"
                if (
                    (
                        current_gap <= cfg.challenge_distance_atr
                        or current_extreme_gap
                        <= cfg.validation_distance_atr
                    )
                    and not latest_validation
                )
                else ("retesting" if pending_retest else "active")
            )
            return {
                "current_role": role,
                "status": status,
                "active": True,
                "current_gap_atr": float(current_gap),
                "break_index": last_break_index,
                "projection_end_index": len(df) - 1,
                "role_reversal_count": reversal_count,
                "role_reversal_confirmed": reversal_count > 0 and not pending_retest,
                "validation_events": [
                    validation_events[index]
                    for index in sorted(validation_events)
                ],
            }

        for event in interval_validations:
            if int(event["index"]) < break_index:
                validation_events[int(event["index"])] = event
        last_break_index = break_index
        role = "resistance" if role == "support" else "support"
        reversal_count += 1
        pending_retest = True
        activation_index = break_index

    raise AssertionError("Unreachable key-zone state")


def _integrity_quality(
    df: pd.DataFrame,
    formation_index: int,
    zone_low: float,
    zone_high: float,
    reversal_count: int,
) -> tuple[float, int, float]:
    closes = df["close"].iloc[formation_index:].to_numpy(float)
    if not len(closes):
        return 1.0, 0, 0.0
    side = np.where(closes > zone_high, 1, np.where(closes < zone_low, -1, 0))
    outside = side[side != 0]
    crossing_count = int(np.sum(outside[1:] != outside[:-1])) if len(outside) > 1 else 0
    inside_ratio = float(np.mean(side == 0))
    unexplained_crossings = max(0, crossing_count - reversal_count)
    quality = 1.0 - 0.55 * inside_ratio - 0.18 * unexplained_crossings
    return float(np.clip(quality, 0.0, 1.0)), crossing_count, inside_ratio


def _score_zone(
    df: pd.DataFrame,
    tests: pd.DataFrame,
    integrity: float,
    cfg: KeyZoneConfig,
    latest_validation_index: int | None = None,
) -> tuple[float, dict[str, float]]:
    n = len(df)
    latest_confirmed = int(tests["confirmed_at_index"].max())
    if latest_validation_index is not None:
        latest_confirmed = max(latest_confirmed, int(latest_validation_index))
    first_pivot = int(tests["pivot_index"].min())
    last_pivot = int(tests["pivot_index"].max())
    components = {
        "tests": float(1.0 - math.exp(-len(tests) / 3.0)),
        "prominence": float(np.clip(tests["prominence_atr"].mean() / 2.5, 0, 1)),
        "rejection": float(np.clip(tests["rejection_atr"].mean() / 3.0, 0, 1)),
        "span": float(np.clip((last_pivot - first_pivot) / max(1, n * 0.50), 0, 1)),
        "recency": float(2.0 ** (-(n - 1 - latest_confirmed) / 75.0)),
        "integrity": float(np.clip(integrity, 0, 1)),
    }
    weighted_score = sum(
        components[name] * weight for name, weight in SCORE_WEIGHTS.items()
    )
    integrity_multiplier = (
        cfg.integrity_score_floor
        + (1.0 - cfg.integrity_score_floor) * components["integrity"]
    )
    components["integrity_multiplier"] = float(integrity_multiplier)
    score = 100.0 * weighted_score * integrity_multiplier
    return round(float(np.clip(score, 0, 100)), 4), components


def detect_key_zones(
    candles: pd.DataFrame,
    config: KeyZoneConfig | None = None,
) -> dict:
    cfg = config or KeyZoneConfig()
    cfg.validate()
    df, pivots = detect_confirmed_pivots(candles, cfg)
    if pivots.empty:
        return {"zones": [], "pivots": [], "data": df}

    pivots = pivots.copy()
    pivots["evidence_source"] = "strict"
    provisional_edge = _provisional_edge_pivots(df, pivots, cfg)
    if not provisional_edge.empty:
        pivots = pd.concat([pivots, provisional_edge], ignore_index=True)
    labels = _dbscan_labels(pivots, cfg)
    pivots["cluster"] = labels
    groups: list[tuple[int, pd.DataFrame]] = [
        (int(cluster_id), pivots[pivots["cluster"] == cluster_id].copy())
        for cluster_id in sorted(label for label in np.unique(labels) if label >= 0)
    ]
    next_cluster_id = max((cluster_id for cluster_id, _ in groups), default=-1) + 1
    claimed_shoulders: set[tuple[str, int]] = set()
    noise_rows = pivots[
        (pivots["cluster"] == -1)
        & (pivots["evidence_source"] == "strict")
    ].sort_values("pivot_index")
    for row_index, anchor in noise_rows.iterrows():
        shoulders = _shoulder_tests_for_anchor(df, anchor, pivots, cfg)
        if not shoulders.empty:
            shoulders = shoulders[
                [
                    (str(row["type"]), int(row["pivot_index"]))
                    not in claimed_shoulders
                    for _, row in shoulders.iterrows()
                ]
            ].copy()
        if shoulders.empty:
            continue
        raw = pd.concat(
            [anchor.to_frame().T.drop(columns=["cluster"]), shoulders],
            ignore_index=True,
        )
        tests = _independent_tests(raw, cfg.min_independent_test_gap)
        if len(tests) < cfg.min_zone_tests:
            continue
        cluster_id = next_cluster_id
        next_cluster_id += 1
        raw["cluster"] = cluster_id
        groups.append((cluster_id, raw))
        pivots.loc[row_index, "cluster"] = cluster_id
        shoulders["cluster"] = cluster_id
        pivots = pd.concat([pivots, shoulders], ignore_index=True)
        claimed_shoulders.update(
            (str(row["type"]), int(row["pivot_index"]))
            for _, row in shoulders.iterrows()
        )

    zones: list[dict] = []
    for cluster_id, raw in groups:
        tests = _independent_tests(raw, cfg.min_independent_test_gap)
        if len(tests) < cfg.min_zone_tests:
            continue
        weights = tests["test_score"].to_numpy(float)
        if float(weights.sum()) <= 0:
            weights = np.ones(len(tests), dtype=float)
        prices = tests["price"].to_numpy(float)
        atr = tests["atr"].to_numpy(float)
        center = float(np.average(prices, weights=weights))
        reference_atr = float(np.average(atr, weights=weights))
        halfwidth = float(
            np.clip(
                max(
                    _weighted_std(prices, weights, center),
                    cfg.min_zone_halfwidth_atr * reference_atr,
                ),
                cfg.min_zone_halfwidth_atr * reference_atr,
                cfg.max_zone_halfwidth_atr * reference_atr,
            )
        )
        zone_low = center - halfwidth
        zone_high = center + halfwidth
        state = _zone_state(df, tests, zone_low, zone_high, cfg)
        formation_index = int(
            tests.iloc[cfg.min_zone_tests - 1]["confirmed_at_index"]
        )
        integrity_start_index = int(tests["pivot_index"].min())
        integrity, crossing_count, inside_ratio = _integrity_quality(
            df,
            integrity_start_index,
            zone_low,
            zone_high,
            int(state["role_reversal_count"]),
        )
        validation_events = list(state["validation_events"])
        validation_indices = [int(event["index"]) for event in validation_events]
        latest_validation_index = (
            max(validation_indices) if validation_indices else None
        )
        score, score_components = _score_zone(
            df,
            tests,
            integrity,
            cfg,
            latest_validation_index,
        )
        current_atr = float(df["atr"].iloc[-1])
        current_close = float(df["close"].iloc[-1])
        distance_atr = abs(current_close - center) / current_atr
        if zone_low <= current_close <= zone_high:
            display_status = "challenging"
            display_active = True
            projection_end_index = len(df) - 1
        else:
            display_status = state["status"]
            display_active = bool(state["active"])
            projection_end_index = int(state["projection_end_index"])
        high_tests = int((tests["type"] == "high").sum())
        low_tests = int((tests["type"] == "low").sum())
        pivot_indices = [int(value) for value in tests["pivot_index"]]
        confirmed_indices = [int(value) for value in tests["confirmed_at_index"]]
        identity = json.dumps(
            [int(cluster_id), pivot_indices, confirmed_indices],
            separators=(",", ":"),
        )
        zone_id = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
        zones.append(
            {
                "id": f"KZ-{zone_id}",
                "center": center,
                "zone_low": zone_low,
                "zone_high": zone_high,
                "halfwidth_atr": halfwidth / reference_atr,
                "score": score,
                "score_components": {
                    name: round(value, 4)
                    for name, value in score_components.items()
                },
                "raw_test_score": float(tests["test_score"].sum()),
                "independent_tests": len(tests),
                "raw_pivots": len(raw),
                "shoulder_tests": int(
                    (tests["evidence_source"] == "shoulder").sum()
                ),
                "provisional_edge_tests": int(
                    (tests["evidence_source"] == "provisional_edge").sum()
                ),
                "provisional_edge_confirmation": bool(
                    (tests["evidence_source"] == "provisional_edge").any()
                ),
                "validation_tests": len(validation_events),
                "validation_indices": validation_indices,
                "latest_validation_index": latest_validation_index,
                "avg_validation_rejection_atr": (
                    float(np.mean([
                        event["rejection_atr"] for event in validation_events
                    ]))
                    if validation_events
                    else 0.0
                ),
                "high_tests": high_tests,
                "low_tests": low_tests,
                "start_index": min(pivot_indices),
                "formation_index": formation_index,
                "integrity_start_index": integrity_start_index,
                "latest_test_index": max(pivot_indices),
                "latest_confirmed_index": max(confirmed_indices),
                # Keep the chart band on the candles that supplied the
                # evidence.  State and scoring still run through the current
                # candle, but a historical zone is not painted as though
                # every later candle had tested it.
                "display_start_index": min(pivot_indices),
                "display_end_index": max(confirmed_indices),
                "projection_end_index": projection_end_index,
                "break_index": state["break_index"],
                "touch_indices": pivot_indices,
                "confirmed_indices": confirmed_indices,
                "current_role": state["current_role"],
                "status": display_status,
                "active": display_active,
                "current_gap_atr": float(state["current_gap_atr"]),
                "distance_from_current_atr": float(distance_atr),
                "role_reversal_confirmed": bool(state["role_reversal_confirmed"]),
                "role_reversal_count": int(state["role_reversal_count"]),
                "crossing_count": crossing_count,
                "inside_close_ratio": inside_ratio,
                "avg_prominence_atr": float(tests["prominence_atr"].mean()),
                "avg_rejection_atr": float(tests["rejection_atr"].mean()),
            }
        )

    zones.sort(
        key=lambda zone: (
            not zone["active"],
            -float(zone["score"]),
            float(zone["distance_from_current_atr"]),
        )
    )
    for rank, zone in enumerate(zones, start=1):
        zone["rank"] = rank
    return {
        "zones": zones,
        "pivots": pivots.to_dict(orient="records"),
        "data": df,
    }


def _attach_dates(zones: list[dict], candles: list[dict]) -> None:
    def date_at(index: int | None) -> str | None:
        if index is None or index < 0 or index >= len(candles):
            return None
        row = candles[index]
        return row.get("end_date") or row.get("date")

    for zone in zones:
        zone["start_date"] = date_at(zone["start_index"])
        zone["formation_date"] = date_at(zone["formation_index"])
        zone["latest_test_date"] = date_at(zone["latest_test_index"])
        zone["latest_confirmed_date"] = date_at(zone["latest_confirmed_index"])
        zone["latest_validation_date"] = date_at(zone["latest_validation_index"])
        zone["break_date"] = date_at(zone["break_index"])


def _select_display_zones(zones: list[dict], per_side: int = 3) -> list[dict]:
    active_zone = [
        zone for zone in zones
        if zone["active"] and zone["status"] == "challenging"
    ]
    selected_ids = {zone["id"] for zone in active_zone}
    selected = list(active_zone)
    for role in ("resistance", "support"):
        candidates = sorted(
            (
                zone for zone in zones
                if zone["active"]
                and zone["current_role"] == role
                and zone["id"] not in selected_ids
            ),
            key=lambda zone: (
                float(zone["distance_from_current_atr"]),
                -float(zone["score"]),
            ),
        )[:per_side]
        selected.extend(candidates)
        selected_ids.update(zone["id"] for zone in candidates)
    return sorted(
        selected,
        key=lambda zone: (
            zone["status"] != "challenging",
            float(zone["distance_from_current_atr"]),
            -float(zone["score"]),
        ),
    )


def analyze_symbol_key_zones(
    symbol: str,
    period: str = "1D",
    limit: int = 150,
    show_weekend_data: str | bool | None = None,
    adjustment: str = "all",
    config: KeyZoneConfig | None = None,
) -> dict:
    clean_period = (period or "1D").upper()
    if clean_period not in SUPPORTED_PERIODS:
        raise ValueError("Unsupported analysis period")
    cfg = config or KeyZoneConfig()
    cfg.validate()
    prepared = _prepare_trendline_analysis(
        symbol,
        clean_period,
        limit,
        show_weekend_data,
        adjustment,
    )
    payload = prepared["market_payload"]
    candles = prepared["candles"]
    window = prepared["window"]
    window_start = int(prepared["window_start"])
    base = {
        "ok": True,
        "algorithm": "key_zones",
        "algorithm_version": KEY_ZONE_ALGORITHM_VERSION,
        "method": "dbscan",
        "config_hash": cfg.signature(),
        "symbol": payload.get("symbol") or symbol,
        "canonical_symbol": (
            payload.get("canonical_symbol")
            or payload.get("symbol")
            or symbol
        ),
        "source": payload.get("source"),
        "period": clean_period,
        "adjustment": str(adjustment or "all"),
        "requested_window_size": int(prepared["requested_window_size"]),
        "window_start_index": window_start,
        "window_size": len(window),
        "data_count": len(candles),
        "latest_data_date": candles[-1]["date"] if candles else None,
        "data_fingerprint": prepared["data_fingerprint"],
        "show_weekend_data": prepared["show_weekend_data"],
        "show_non_us_market_days": prepared["show_weekend_data"],
    }
    if len(window) < 30:
        return {
            **base,
            "zones": [],
            "message": "K线数量不足，无法识别水平支撑/压力区。",
        }

    cache_key = (
        KEY_ZONE_ALGORITHM_VERSION,
        base["canonical_symbol"],
        clean_period,
        base["requested_window_size"],
        base["show_weekend_data"],
        adjustment,
        base["data_fingerprint"],
        cfg.signature(),
    )
    cached = _cached_result(cache_key)
    if cached is not None:
        return cached

    result = detect_key_zones(candles_to_dataframe(window), cfg)
    zones = result["zones"]
    _attach_dates(zones, window)
    for zone in zones:
        for field in (
            "start_index",
            "formation_index",
            "integrity_start_index",
            "latest_test_index",
            "latest_confirmed_index",
            "display_start_index",
            "display_end_index",
            "projection_end_index",
            "break_index",
            "latest_validation_index",
        ):
            if zone[field] is not None:
                zone[field] += window_start
        zone["touch_indices"] = [
            index + window_start for index in zone["touch_indices"]
        ]
        zone["confirmed_indices"] = [
            index + window_start for index in zone["confirmed_indices"]
        ]
        zone["validation_indices"] = [
            index + window_start for index in zone["validation_indices"]
        ]
    display_zones = _select_display_zones(zones)
    response = {
        **base,
        "zones": display_zones,
        "detected_zone_count": len(zones),
        "message": (
            None
            if display_zones
            else "未识别出满足阈值的水平支撑/压力区。"
        ),
    }
    _store_result(cache_key, response)
    return deepcopy(response)
