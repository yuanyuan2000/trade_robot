from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from services.backtest.validation import default_backtest_settings, validate_settings


REALTIME_SETTINGS_KEYS = ("leverage_multiplier", "dynamic_leverage")
HISTORICAL_ONLY_SETTINGS_KEYS = {
    "start_date", "end_date", "initial_capital", "commission_per_share",
    "minimum_commission", "slippage_bps", "allow_fractional_shares",
    "benchmark", "risk_free_rate", "strict_data", "generate_logs",
}
REALTIME_MODEL_NOTIONAL = 100_000.0


def realtime_settings_from(values: dict | None) -> dict:
    """Return the validated subset that can affect advisory decisions."""
    source = dict(values or {})
    validated = validate_settings({**default_backtest_settings(), **source})
    return {
        "leverage_multiplier": validated["leverage_multiplier"],
        "dynamic_leverage": deepcopy(validated["dynamic_leverage"]),
    }


def validate_realtime_overrides(values: dict | None) -> dict:
    """Validate an override patch without expanding omitted values."""
    source = dict(values or {})
    # Old API clients sent the full backtest settings object. Accept those
    # recognized fields during migration, but never persist them in realtime.
    unknown = set(source) - set(REALTIME_SETTINGS_KEYS) - HISTORICAL_ONLY_SETTINGS_KEYS
    if unknown:
        raise ValueError(f"实时决策包含不支持的运行设置：{sorted(unknown)}。")
    validated = realtime_settings_from({
        key: source[key] for key in REALTIME_SETTINGS_KEYS if key in source
    })
    result: dict[str, Any] = {}
    if "leverage_multiplier" in source:
        result["leverage_multiplier"] = validated["leverage_multiplier"]
    if "dynamic_leverage" in source:
        raw_dynamic = source["dynamic_leverage"]
        if not isinstance(raw_dynamic, dict):
            raise ValueError("动态杠杆设置必须是对象。")
        allowed = set(validated["dynamic_leverage"])
        unknown_dynamic = set(raw_dynamic) - allowed
        if unknown_dynamic:
            raise ValueError(f"动态杠杆包含未知设置：{sorted(unknown_dynamic)}。")
        result["dynamic_leverage"] = {
            key: validated["dynamic_leverage"][key]
            for key in raw_dynamic
        }
    return result


def merge_realtime_settings(*values: dict | None) -> dict:
    merged: dict[str, Any] = {}
    dynamic: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        if "leverage_multiplier" in value:
            merged["leverage_multiplier"] = value["leverage_multiplier"]
        if isinstance(value.get("dynamic_leverage"), dict):
            dynamic.update(value["dynamic_leverage"])
    if dynamic:
        merged["dynamic_leverage"] = dynamic
    return realtime_settings_from(merged)


def realtime_engine_settings(values: dict | None, trading_date: str) -> dict:
    """Build neutral internal settings for the shared strategy evaluator.

    The notional only gives the existing percentage-based engine a scale. It is
    never exposed as task capital and cannot affect fills: realtime advisory
    mode always uses zero costs and fractional quantities.
    """
    realtime = realtime_settings_from(values)
    return {
        **default_backtest_settings(),
        **realtime,
        "start_date": trading_date,
        "end_date": trading_date,
        "initial_capital": REALTIME_MODEL_NOTIONAL,
        "commission_per_share": 0.0,
        "minimum_commission": 0.0,
        "slippage_bps": 0.0,
        "allow_fractional_shares": True,
        "benchmark": "none",
        "strict_data": True,
        "generate_logs": True,
    }


def empty_recommendation_state() -> dict:
    return {
        "state_version": 2,
        "recommended_targets": {},
        "recommended_exposures": {},
        "configured_symbol_leverage_multipliers": {},
        "symbol_leverage_multipliers": {},
    }


def normalize_recommendation_state(value: dict | None) -> dict:
    """Read new target state and migrate legacy simulated portfolio snapshots."""
    def bounded_multiplier(raw_value: Any, maximum: float) -> float:
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(number):
            return 1.0
        return min(maximum, max(1.0, number))

    raw = dict(value or {})
    if int(raw.get("state_version") or 0) >= 2:
        effective = {
            str(symbol): bounded_multiplier(multiplier, 100.0)
            for symbol, multiplier in (
                raw.get("symbol_leverage_multipliers") or {}
            ).items()
        }
        configured = {
            str(symbol): bounded_multiplier(multiplier, 10.0)
            for symbol, multiplier in (
                raw.get("configured_symbol_leverage_multipliers") or {}
            ).items()
        }
        # Early v2 snapshots could contain an effective value but omit its
        # configured-layer counterpart. Treat each such value as the last
        # configured dynamic leverage. Otherwise the next recalculation
        # divides it by an unrelated manual default and can infer a sub-one
        # (or compounding) strategy layer.
        for symbol, multiplier in effective.items():
            if symbol not in configured:
                # Missing configured state is the signature of early v2. Its
                # effective value was repeatedly compounded in some runs and
                # can already exceed Portfolio's 1..100 runtime guard. There
                # was no separately recoverable strategy layer, so reset both
                # values to the valid dynamic layer instead of retaining the
                # corrupted product.
                configured[symbol] = bounded_multiplier(multiplier, 10.0)
                effective[symbol] = configured[symbol]
        return {
            **empty_recommendation_state(),
            **raw,
            "recommended_targets": dict(raw.get("recommended_targets") or {}),
            "recommended_exposures": dict(raw.get("recommended_exposures") or {}),
            "configured_symbol_leverage_multipliers": configured,
            "symbol_leverage_multipliers": effective,
        }

    targets = dict(raw.get("strategy_target_weights") or {})
    exposures: dict[str, float] = {}
    for symbol, item in (raw.get("positions") or {}).items():
        if not isinstance(item, dict):
            continue
        if symbol not in targets and item.get("strategy_weight") is not None:
            targets[str(symbol)] = float(item["strategy_weight"])
        if item.get("weight") is not None:
            exposures[str(symbol)] = float(item["weight"])
    # Legacy strategy weights are stored as fractions. New state is percent.
    normalized_targets = {
        str(symbol): float(weight) * 100.0
        for symbol, weight in targets.items()
        if float(weight) > 0
    }
    normalized_exposures = {
        str(symbol): float(weight) * 100.0
        for symbol, weight in exposures.items()
        if float(weight) > 0
    }
    legacy_configured = {
        str(symbol): bounded_multiplier(multiplier, 10.0)
        for symbol, multiplier in (
            raw.get("configured_symbol_leverage_multipliers") or {}
        ).items()
    }
    legacy_effective = {
        str(symbol): bounded_multiplier(multiplier, 100.0)
        for symbol, multiplier in (
            raw.get("symbol_leverage_multipliers") or {}
        ).items()
    }
    for symbol, multiplier in legacy_effective.items():
        if symbol not in legacy_configured:
            legacy_configured[symbol] = bounded_multiplier(multiplier, 10.0)
            legacy_effective[symbol] = legacy_configured[symbol]
    return {
        **empty_recommendation_state(),
        "recommended_targets": normalized_targets,
        "recommended_exposures": normalized_exposures,
        "configured_symbol_leverage_multipliers": legacy_configured,
        "symbol_leverage_multipliers": legacy_effective,
    }
