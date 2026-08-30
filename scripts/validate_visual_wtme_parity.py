from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math

from database import backtest_repository
from services.backtest.code_strategies import RapidDropWtmeRotationStrategy
from services.backtest.engine import BacktestEngine
from services.backtest.validation import validate_strategy_payload


TRADE_FIELDS = (
    "event_time",
    "symbol",
    "side",
    "quantity",
    "reference_price",
    "fill_price",
    "gross_amount",
    "commission",
    "slippage_amount",
    "realized_pnl",
    "cash_after",
    "position_quantity_after",
    "position_value_after",
    "position_weight_after",
)


def equivalent_visual_strategy(code_strategy: dict) -> dict:
    if code_strategy.get("code_key") != RapidDropWtmeRotationStrategy.key:
        raise ValueError("只能转换急跌回避与 WTME 动量轮动代码策略。")
    params = RapidDropWtmeRotationStrategy.validate_params(
        code_strategy["definition"].get("params", {})
    )
    if params["buy_top_n"] != 1 or params["buy_score_threshold"] != 9999:
        raise ValueError(
            "非代码 competition 模式只能交叉验证默认的 WTME 买入条件：前 1 名或评分 > 9999。"
        )
    risk_expression = (
        f"rapid_drop({params['drop_lookback_sessions']}, "
        f"{params['drop_threshold_percent']:g})"
    )
    rules = []
    eligibility = "true"
    if params["enable_percent_drop_filter"]:
        rules.append({
            "id": "rapid-drop-exit",
            "name": "百分比急跌时退出并当日回避",
            "enabled": True,
            "priority": 10,
            "action": "SELL",
            "sizing_mode": "TARGET",
            "value": 0,
            "condition": f"{risk_expression} = 1",
            "when": params["risk_check_time"],
        })
        eligibility = f"{risk_expression} = 0"
    strategy = {
        "name": f"{code_strategy['name']}（非代码等价验证）",
        "description": "用于代码/非代码回测交叉验证，不写入策略数据库。",
        "design_mode": "visual",
        "selection_mode": "competition",
        "definition": {
            "symbols": deepcopy(code_strategy["definition"]["symbols"]),
            "rules": rules,
            "competition": {
                "eligibility": eligibility,
                "eligibility_when": params["risk_check_time"],
                "score": (
                    f"wtme({params['wtme_period']}, {params['wtme_half_life']:g}, "
                    f"{params['wtme_epsilon']:g})"
                ),
                "minimum_score": None,
                "target_weight": 100,
                "cash_when_none": True,
                "rebalance_existing": False,
                "when": params["selection_time"],
            },
        },
        "default_settings": deepcopy(code_strategy.get("default_settings") or {}),
    }
    return validate_strategy_payload(strategy)


def _latest_completed_run_id() -> int:
    strategies = [
        strategy for strategy in backtest_repository.list_strategies()
        if strategy.get("code_key") == RapidDropWtmeRotationStrategy.key
    ]
    for strategy in strategies:
        completed = [
            run for run in backtest_repository.list_runs(strategy["id"], limit=200)
            if run["status"] == "completed"
        ]
        if completed:
            return int(completed[0]["id"])
    raise ValueError("没有可用于核验的已完成 WTME 代码策略历史运行。")


def _assert_equal(label: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{label} 不一致。")


def _trade_projection(trades: list[dict]) -> list[tuple]:
    return [tuple(trade.get(field) for field in TRADE_FIELDS) for trade in trades]


def _equity_projection(points: list[dict]) -> list[dict]:
    fields = (
        "sequence", "trading_date", "cash", "receivables", "positions_value",
        "equity", "borrowed_cash", "gross_leverage", "return_rate",
        "drawdown_rate", "benchmark_equity", "benchmark_return_rate", "positions",
    )
    return [{field: point.get(field) for field in fields} for point in points]


def _assert_metrics_close(actual: dict, expected: dict) -> None:
    if actual.keys() != expected.keys():
        raise AssertionError("指标字段不一致。")
    for key in actual:
        left = actual[key]
        right = expected[key]
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if not math.isclose(
                float(left),
                float(right),
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise AssertionError(f"指标 {key} 不一致：{left} != {right}")
        elif left != right:
            raise AssertionError(f"指标 {key} 不一致：{left!r} != {right!r}")


def validate(run_id: int) -> dict:
    stored_run = backtest_repository.get_run(run_id)
    if stored_run["status"] != "completed":
        raise ValueError("指定运行不是已完成状态。")
    code_strategy = stored_run["strategy_snapshot"]
    if code_strategy.get("code_key") != RapidDropWtmeRotationStrategy.key:
        raise ValueError("指定运行不是急跌回避与 WTME 动量轮动代码策略。")
    settings = stored_run["settings"]

    code_engine = BacktestEngine(code_strategy, settings)
    code_result = code_engine.run()
    visual_strategy = equivalent_visual_strategy(code_strategy)
    visual_result = BacktestEngine(
        visual_strategy,
        settings,
        dataset=code_engine.dataset,
    ).run()

    _assert_equal(
        "代码版与非代码版逐笔成交",
        _trade_projection(visual_result.trades),
        _trade_projection(code_result.trades),
    )
    _assert_equal(
        "代码版与非代码版每日权益",
        _equity_projection(visual_result.equity_points),
        _equity_projection(code_result.equity_points),
    )
    _assert_metrics_close(visual_result.metrics, code_result.metrics)

    stored_trades = backtest_repository.get_trades(run_id)
    stored_equity = backtest_repository.get_equity_points(run_id)
    _assert_equal(
        "代码版重跑与历史运行逐笔成交",
        _trade_projection(code_result.trades),
        _trade_projection(stored_trades),
    )
    _assert_equal(
        "代码版重跑与历史运行每日权益",
        _equity_projection(code_result.equity_points),
        _equity_projection(stored_equity),
    )
    _assert_metrics_close(code_result.metrics, stored_run["metrics"])

    return {
        "ok": True,
        "run_id": run_id,
        "strategy": code_strategy["name"],
        "start_date": settings["start_date"],
        "end_date": settings["end_date"],
        "sessions": len(code_result.equity_points),
        "trades": len(code_result.trades),
        "ending_equity": code_result.metrics["ending_equity"],
        "total_return": code_result.metrics["total_return"],
        "checks": [
            "visual_vs_code_trades",
            "visual_vs_code_equity",
            "visual_vs_code_metrics",
            "code_vs_stored_trades",
            "code_vs_stored_equity",
            "code_vs_stored_metrics",
        ],
        "visual_strategy": visual_strategy,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="交叉验证非代码 WTME 竞争策略、代码策略和已保存历史结果。"
    )
    parser.add_argument("--run-id", type=int, help="已完成的 WTME 代码策略运行 ID")
    args = parser.parse_args()
    result = validate(args.run_id or _latest_completed_run_id())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
