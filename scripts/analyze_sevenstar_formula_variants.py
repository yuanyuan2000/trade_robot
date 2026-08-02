from __future__ import annotations

import argparse
from bisect import bisect_left
from collections.abc import Callable
from copy import deepcopy
import json
import math
import time

import numpy as np

from database import backtest_repository
from services.backtest.code_strategies import (
    STRATEGY_REGISTRY,
    SevenStarEtfRotationStrategy,
)
from services.backtest.engine import BacktestEngine


TrendFunction = Callable[[list[float], int], tuple[float, float, float]]


def _components(
    prices: list[float],
    lookback: int,
    *,
    fit_weights: np.ndarray,
    statistic_weights: np.ndarray,
    weighted_center: bool,
) -> tuple[float, float, float]:
    recent = np.asarray(prices[-(lookback + 1) :], dtype=float)
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1, w=fit_weights)
    fitted = slope * x + intercept
    center = (
        float(np.average(y, weights=statistic_weights))
        if weighted_center
        else float(np.mean(y))
    )
    ss_res = float(np.sum(statistic_weights * (y - fitted) ** 2))
    ss_tot = float(np.sum(statistic_weights * (y - center) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot else 0.0
    return float(slope), math.exp(float(slope) * 250) - 1, r_squared


def _original_components(
    prices: list[float], lookback: int
) -> tuple[float, float, float]:
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    return _components(
        prices,
        lookback,
        fit_weights=weights,
        statistic_weights=weights,
        weighted_center=False,
    )


def legacy_original(prices: list[float], lookback: int):
    _, annualized, r_squared = _original_components(prices, lookback)
    return annualized, r_squared, annualized * r_squared


class AuditSevenStarStrategy(SevenStarEtfRotationStrategy):
    """Run formula variants with the pre-v1.0.1 eligibility semantics."""

    def _metrics(self, context, symbol: str) -> dict:
        result = super()._metrics(context, symbol)
        if "non_positive_trend" in result["filter_codes"]:
            index = result["filter_codes"].index("non_positive_trend")
            result["filter_codes"].pop(index)
            result["filter_reasons"].pop(index)
            result["eligible"] = not result["filter_codes"]
        return result


def regression_weight_only(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=np.sqrt(weights),
        statistic_weights=weights,
        weighted_center=False,
    )
    return annualized, r_squared, annualized * r_squared


def weighted_mean_only(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=weights,
        statistic_weights=weights,
        weighted_center=True,
    )
    return annualized, r_squared, annualized * r_squared


def r_squared_clip_only(prices: list[float], lookback: int):
    _, annualized, r_squared = _original_components(prices, lookback)
    r_squared = min(1.0, max(0.0, r_squared))
    return annualized, r_squared, annualized * r_squared


def log_annual_score_only(prices: list[float], lookback: int):
    slope, annualized, r_squared = _original_components(prices, lookback)
    return annualized, r_squared, slope * 250 * r_squared


def fitted_window_score_only(prices: list[float], lookback: int):
    slope, annualized, r_squared = _original_components(prices, lookback)
    return annualized, r_squared, math.expm1(slope * lookback) * r_squared


def coherent_effective_w2(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=weights,
        statistic_weights=weights**2,
        weighted_center=True,
    )
    r_squared = min(1.0, max(0.0, r_squared))
    return annualized, r_squared, annualized * r_squared


def coherent_effective_w2_log_score(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=weights,
        statistic_weights=weights**2,
        weighted_center=True,
    )
    r_squared = min(1.0, max(0.0, r_squared))
    return annualized, r_squared, slope * 250 * r_squared


def coherent_linear_w(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=np.sqrt(weights),
        statistic_weights=weights,
        weighted_center=True,
    )
    r_squared = min(1.0, max(0.0, r_squared))
    return annualized, r_squared, annualized * r_squared


def coherent_linear_w_log_score(prices: list[float], lookback: int):
    count = min(len(prices), lookback + 1)
    weights = np.linspace(1.0, 2.0, count)
    slope, annualized, r_squared = _components(
        prices,
        lookback,
        fit_weights=np.sqrt(weights),
        statistic_weights=weights,
        weighted_center=True,
    )
    r_squared = min(1.0, max(0.0, r_squared))
    return annualized, r_squared, slope * 250 * r_squared


def _score_logs(result) -> list[dict]:
    return [
        {"date": item["event_time"][:10], **item["context"]}
        for item in result.logs
        if item["event_type"] == "SEVENSTAR_DAILY_SCORE"
    ]


def _selected_by_date(items: list[dict]) -> dict[str, tuple[str, ...]]:
    selected: dict[str, list[str]] = {}
    for item in items:
        if item.get("selected_for_target"):
            selected.setdefault(item["date"], []).append(item["etf"])
    return {key: tuple(value) for key, value in selected.items()}


def _year_returns(result, initial_capital: float) -> dict[str, float]:
    year_end: dict[str, float] = {}
    for point in result.equity_points:
        year_end[point["trading_date"][:4]] = float(point["equity"])
    returns: dict[str, float] = {}
    previous = initial_capital
    for year, ending in sorted(year_end.items()):
        returns[year] = ending / previous - 1
        previous = ending
    return returns


def _summary(result, *, baseline_selected: dict[str, tuple[str, ...]] | None) -> dict:
    metrics = result.metrics
    items = _score_logs(result)
    selected = _selected_by_date(items)
    selected_items = [item for item in items if item.get("selected_for_target")]
    all_dates = set(selected)
    if baseline_selected is not None:
        all_dates |= set(baseline_selected)
    changed = (
        None
        if baseline_selected is None
        else sum(selected.get(day, ()) != baseline_selected.get(day, ()) for day in all_dates)
    )
    return {
        "total_return": metrics["total_return"],
        "annualized_return": metrics["annualized_return"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe_ratio": metrics["sharpe_ratio"],
        "sortino_ratio": metrics["sortino_ratio"],
        "calmar_ratio": metrics["calmar_ratio"],
        "ending_equity": metrics["ending_equity"],
        "trade_count": metrics["trade_count"],
        "turnover": metrics["turnover"],
        "total_commission": metrics["total_commission"],
        "average_exposure": metrics["average_exposure"],
        "negative_r2_observations": sum(item["r_squared"] < 0 for item in items),
        "double_negative_selected_days": sum(
            item["annualized_returns"] < 0
            and item["r_squared"] < 0
            and item.get("selected_for_target")
            for item in items
        ),
        "selected_signal_days": len(selected),
        "changed_signal_days_vs_baseline": changed,
        "max_selected_annualized": max(
            (item["annualized_returns"] for item in selected_items), default=None
        ),
        "max_selected_score": max(
            (item["score"] for item in selected_items), default=None
        ),
        "year_returns": _year_returns(result, metrics["initial_capital"]),
    }


def _install_fast_bounded_history(dataset, history_limit: int) -> None:
    """Preserve SevenStar semantics while avoiding repeated full-history scans."""
    dates = {
        symbol: [row["date"] for row in rows]
        for symbol, rows in dataset.daily.items()
    }
    splits: dict[str, list[dict]] = {}
    for action in dataset.corporate_actions:
        if action["action_type"] in {"forward_split", "reverse_split"}:
            splits.setdefault(action["symbol"], []).append(action)
    cache: dict[tuple[str, str], list[dict]] = {}

    def daily_before(symbol: str, trading_date: str) -> list[dict]:
        key = (symbol, trading_date)
        if key in cache:
            return cache[key]
        rows = dataset.daily[symbol]
        end = bisect_left(dates[symbol], trading_date)
        selected = rows[max(0, end - history_limit) : end]
        adjusted = []
        for source in selected:
            factor = 1.0
            for action in splits.get(symbol, []):
                effective = action.get("ex_date") or action["process_date"]
                if source["date"] < effective <= trading_date:
                    old_rate = float(action["old_rate"])
                    new_rate = float(action["new_rate"])
                    if old_rate <= 0 or new_rate <= 0:
                        raise ValueError(f"{symbol} has an invalid split ratio")
                    factor *= new_rate / old_rate
            row = dict(source)
            if abs(factor - 1.0) >= 1e-15:
                for field in ("open", "high", "low", "close"):
                    row[field] = float(row[field]) / factor
                row["volume"] = float(row.get("volume") or 0) * factor
            adjusted.append(row)
        cache[key] = adjusted
        return adjusted

    dataset.daily_before = daily_before


def _manifest_signature(manifest: dict) -> dict:
    return {
        "start_date": manifest.get("start_date"),
        "end_date": manifest.get("end_date"),
        "sessions": manifest.get("sessions"),
        "market_calendar_sha256": manifest.get("market_calendar_sha256"),
        "corporate_actions_sha256": manifest.get("corporate_actions_sha256"),
        "symbols": {
            symbol: {
                key: details.get(key)
                for key in (
                    "daily_rows",
                    "daily_sha256",
                    "minute_points_loaded",
                    "minute_sha256",
                    "cumulative_volume_points",
                    "cumulative_volume_sha256",
                    "eligible_start_date",
                )
            }
            for symbol, details in sorted((manifest.get("symbols") or {}).items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=17)
    parser.add_argument("--allow-data-drift", action="store_true")
    args = parser.parse_args()

    reference = backtest_repository.get_run(args.run_id)
    strategy = deepcopy(reference["strategy_snapshot"])
    reference_code_version = strategy.get("code_version")
    strategy["code_version"] = AuditSevenStarStrategy.version
    settings = reference["settings"]
    production = SevenStarEtfRotationStrategy._weighted_trend
    registered_strategy = STRATEGY_REGISTRY[SevenStarEtfRotationStrategy.key]

    print(
        json.dumps(
            {
                "reference_run_id": args.run_id,
                "reference_code_version": reference_code_version,
                "execution_code_version": strategy["code_version"],
                "strategy": strategy["name"],
                "symbols": [item["symbol"] for item in strategy["definition"]["symbols"]],
                "settings": settings,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    STRATEGY_REGISTRY[SevenStarEtfRotationStrategy.key] = AuditSevenStarStrategy
    try:
        load_started = time.perf_counter()
        AuditSevenStarStrategy._weighted_trend = staticmethod(legacy_original)
        baseline_engine = BacktestEngine(strategy, settings)
        dataset = baseline_engine.dataset
        data_matches_reference = _manifest_signature(
            dataset.manifest
        ) == _manifest_signature(reference["data_manifest"])
        if not data_matches_reference and not args.allow_data_drift:
            raise RuntimeError(
                "Current local data does not match the reference run manifest; "
                "pass --allow-data-drift only for an intentional new experiment."
            )
        history_limit = AuditSevenStarStrategy.minimum_lookback(
            strategy["definition"]["params"]
        )
        _install_fast_bounded_history(dataset, history_limit)
        print(
            json.dumps(
                {
                    "dataset_loaded_seconds": time.perf_counter() - load_started,
                    "sessions": len(dataset.sessions),
                    "calendar_sha256": dataset.manifest.get("market_calendar_sha256"),
                    "corporate_actions_sha256": dataset.manifest.get(
                        "corporate_actions_sha256"
                    ),
                    "history_limit": history_limit,
                    "data_matches_reference": data_matches_reference,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        variants: list[tuple[str, TrendFunction, BacktestEngine | None]] = [
            ("baseline_legacy_v1.0.0", legacy_original, baseline_engine),
            ("regression_weight_only", regression_weight_only, None),
            ("weighted_mean_only", weighted_mean_only, None),
            ("r_squared_clip_only", r_squared_clip_only, None),
            ("log_annual_score_only", log_annual_score_only, None),
            ("fitted_window_score_only", fitted_window_score_only, None),
            ("coherent_effective_w2", coherent_effective_w2, None),
            ("production_consistent_w2", production, None),
            ("coherent_effective_w2_log_score", coherent_effective_w2_log_score, None),
            ("coherent_linear_w", coherent_linear_w, None),
            ("coherent_linear_w_log_score", coherent_linear_w_log_score, None),
        ]
        baseline_selected = None
        for name, function, prepared_engine in variants:
            AuditSevenStarStrategy._weighted_trend = staticmethod(function)
            engine = prepared_engine or BacktestEngine(strategy, settings, dataset=dataset)
            started = time.perf_counter()
            result = engine.run()
            summary = _summary(result, baseline_selected=baseline_selected)
            if baseline_selected is None:
                baseline_selected = _selected_by_date(_score_logs(result))
            print(
                json.dumps(
                    {"variant": name, "seconds": time.perf_counter() - started, **summary},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        STRATEGY_REGISTRY[SevenStarEtfRotationStrategy.key] = registered_strategy


if __name__ == "__main__":
    main()
