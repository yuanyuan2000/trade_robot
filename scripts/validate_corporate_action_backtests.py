"""Run repeatable real-data checks across representative corporate actions."""

from __future__ import annotations

import json

from services.backtest.engine import BacktestEngine
from services.backtest.validation import default_strategy_payload
from services.corporate_action_adjustment_service import adjust_price_rows


SCENARIOS = (
    ("MAGS", "2023-11-01", "2024-01-05", {"name_change", "cash_dividend"}),
    ("VUG", "2026-04-15", "2026-04-24", {"forward_split"}),
    ("NVDA", "2021-07-14", "2021-07-26", {"forward_split"}),
    ("NVDA", "2024-06-03", "2024-06-17", {"forward_split", "cash_dividend"}),
    ("USO", "2020-04-22", "2020-05-06", {"reverse_split"}),
)


def _strategy(symbol: str) -> dict:
    strategy = default_strategy_payload(
        name=f"{symbol} 公司行动审计",
        design_mode="visual",
        selection_mode="single",
    )
    strategy["definition"] = {
        "symbols": [{"symbol": symbol, "max_weight": 100}],
        "rules": [
            {
                "id": "buy-and-hold",
                "name": "期初买入并持有",
                "enabled": True,
                "priority": 10,
                "action": "BUY",
                "sizing_mode": "TARGET",
                "value": 100,
                # Tautology intentionally evaluates a real indicator so the
                # point-in-time adjusted history path is covered as well as
                # raw-bar fills and portfolio accounting.
                "condition": "price > ma(2) OR price <= ma(2)",
                "when": "OPEN",
            }
        ],
    }
    return strategy


def _maximum_change(values: list[float]) -> float:
    changes = [
        abs(current / previous - 1)
        for previous, current in zip(values, values[1:])
        if previous
    ]
    return max(changes, default=0.0)


def run() -> list[dict]:
    results = []
    for symbol, start_date, end_date, expected_types in SCENARIOS:
        strategy = _strategy(symbol)
        settings = {
            **strategy["default_settings"],
            "start_date": start_date,
            "end_date": end_date,
            "commission_per_share": 0,
            "minimum_commission": 0,
            "slippage_bps": 0,
            "allow_fractional_shares": True,
            "benchmark": "none",
        }
        engine = BacktestEngine(strategy, settings)
        action_types = {
            action["action_type"]
            for action in engine.dataset.corporate_actions
        }
        missing = expected_types - action_types
        if missing:
            raise AssertionError(f"{symbol} 未识别预期公司行动：{sorted(missing)}")

        adjusted_rows = adjust_price_rows(
            [
                row for row in engine.dataset.daily[symbol]
                if start_date <= row["date"] <= end_date
            ],
            engine.dataset.corporate_actions,
            mode="all",
        )
        chart_jump = _maximum_change(
            [float(row["close"]) for row in adjusted_rows]
        )
        if chart_jump >= 0.25:
            raise AssertionError(f"{symbol} 复权 K 线仍有异常跳变：{chart_jump:.4%}")

        result = engine.run()
        equity_jump = _maximum_change(
            [settings["initial_capital"]]
            + [float(point["equity"]) for point in result.equity_points]
        )
        if equity_jump >= 0.25:
            raise AssertionError(f"{symbol} 回测权益仍有异常跳变：{equity_jump:.4%}")
        results.append(
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "actions": sorted(action_types),
                "total_return": result.metrics["total_return"],
                "maximum_chart_change": chart_jump,
                "maximum_equity_change": equity_jump,
            }
        )
    return results


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
