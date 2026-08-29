from __future__ import annotations

from collections import deque
import json
import math

from database import intraday_repository, repository
from services.backtest.data import HistoricalDataSet, _epoch_minute
from services.backtest.engine import BacktestEngine


START_DATE = "2026-07-01"
END_DATE = "2026-07-21"
UNIVERSE = ["SPY", "GLD", "NVDA", "MU", "XLE"]
INITIAL_CAPITAL = 100_000.0
COMMISSION_PER_SHARE = 0.005
MINIMUM_COMMISSION = 1.0
SLIPPAGE_BPS = 2.0


def settings(benchmark: str = "auto") -> dict:
    return {
        "start_date": START_DATE,
        "end_date": END_DATE,
        "initial_capital": INITIAL_CAPITAL,
        "commission_per_share": COMMISSION_PER_SHARE,
        "minimum_commission": MINIMUM_COMMISSION,
        "slippage_bps": SLIPPAGE_BPS,
        "allow_fractional_shares": False,
        "benchmark": benchmark,
        "risk_free_rate": 0,
        "strict_data": True,
    }


def load_real_dataset(
    symbols: list[str],
    *,
    intraday_events: list[str] | None = None,
) -> HistoricalDataSet:
    daily = {symbol: repository.get_daily_prices(symbol) for symbol in symbols}
    sessions = [
        row["date"]
        for row in daily[symbols[0]]
        if START_DATE <= row["date"] <= END_DATE
    ]
    if not sessions:
        raise AssertionError("真实行情验证区间没有交易日。")
    for symbol, rows in daily.items():
        date_map = {row["date"]: row for row in rows}
        absent = [trading_date for trading_date in sessions if trading_date not in date_map]
        if absent:
            raise AssertionError(f"{symbol} 缺少日线：{absent}")
        scoped = [
            row
            for row in rows
            if "2026-06-01" <= row["date"] <= END_DATE
        ]
        for previous, current in zip(scoped, scoped[1:]):
            ratio = float(current["open"]) / float(previous["close"])
            if not 0.7 <= ratio <= 1.3:
                raise AssertionError(
                    f"{symbol} 在验证窗口存在疑似未处理公司行动："
                    f"{previous['date']}->{current['date']} ratio={ratio}"
                )
    events = intraday_events or []
    exact = [event for event in events if event not in {"OPEN", "CLOSE"}]
    minute: dict[str, dict[int, dict]] = {}
    if exact:
        required = sorted(
            {
                minute_value
                for trading_date in sessions
                for event in exact
                for minute_value in (
                    _epoch_minute(trading_date, event) - 1,
                    _epoch_minute(trading_date, event),
                )
            }
        )
        for symbol in symbols:
            minute[symbol] = intraday_repository.get_minute_bars_at(symbol, required)
            absent = [value for value in required if value not in minute[symbol]]
            if absent:
                raise AssertionError(
                    f"{symbol} 缺少 {len(absent)} 个验证所需分钟点。"
                )
    return HistoricalDataSet(
        daily=daily,
        sessions=sessions,
        minute=minute,
        required_intraday_events=events,
        corporate_actions=[],
        manifest={
            "validation": "local real raw bars",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "sessions": len(sessions),
            "symbols": symbols,
            "intraday_events": events,
        },
    )


def rule(
    rule_id: str,
    action: str,
    condition: str,
    *,
    value: float,
    priority: int,
    when: str = "OPEN",
) -> dict:
    return {
        "id": rule_id,
        "name": rule_id,
        "enabled": True,
        "priority": priority,
        "action": action,
        "sizing_mode": "TARGET",
        "value": value,
        "condition": condition,
        "when": when,
    }


def visual_strategy(
    name: str,
    mode: str,
    symbols: list[dict],
    rules: list[dict],
    competition: dict | None = None,
) -> dict:
    definition = {"symbols": symbols, "rules": rules}
    if competition:
        definition["competition"] = competition
    return {
        "name": name,
        "design_mode": "visual",
        "selection_mode": mode,
        "definition": definition,
        "default_settings": settings(),
    }


def validate_benchmark(
    result,
    dataset: HistoricalDataSet,
    weights: dict[str, float],
) -> int:
    cash = INITIAL_CAPITAL
    quantities = {symbol: 0 for symbol in weights}
    first_date = dataset.sessions[0]
    opening_marks = {
        symbol: float(dataset.day_bar(symbol, first_date)["open"])
        for symbol in weights
    }
    for symbol, weight_percent in weights.items():
        reference = opening_marks[symbol]
        fill = reference * (1 + SLIPPAGE_BPS / 10_000)
        current_equity = cash + sum(
            quantities[item] * opening_marks[item]
            for item in weights
        )
        current_value = quantities[symbol] * reference
        target = weight_percent / 100
        best = 0
        maximum = int(cash / fill)
        for quantity in range(maximum + 1):
            commission = (
                max(quantity * COMMISSION_PER_SHARE, MINIMUM_COMMISSION)
                if quantity
                else 0
            )
            total = quantity * fill + commission
            slippage = quantity * (fill - reference)
            projected_equity = current_equity - commission - slippage
            projected_value = current_value + quantity * reference
            if (
                total <= cash + 1e-8
                and projected_equity > 0
                and projected_value / projected_equity <= target + 1e-8
            ):
                best = quantity
        commission = (
            max(best * COMMISSION_PER_SHARE, MINIMUM_COMMISSION)
            if best
            else 0
        )
        cash -= best * fill + commission
        quantities[symbol] += best

    checks = 0
    for point in result.equity_points:
        expected = cash + sum(
            quantities[symbol]
            * float(dataset.day_bar(symbol, point["trading_date"])["close"])
            for symbol in weights
        )
        if not math.isclose(
            float(point["benchmark_equity"]),
            expected,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise AssertionError(
                f"基准买入持有权益错误：expected={expected}, point={point}"
            )
        if not math.isclose(
            float(point["benchmark_return_rate"]),
            expected / INITIAL_CAPITAL - 1,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise AssertionError(f"基准收益率错误：{point}")
        checks += 1
    return checks


def validate_accounting(
    result,
    dataset: HistoricalDataSet,
    *,
    max_weights: dict[str, float],
    benchmark_weights: dict[str, float],
) -> dict:
    previous_cash = INITIAL_CAPITAL
    lots: dict[str, deque[list[float]]] = {
        symbol: deque()
        for symbol in max_weights
    }
    pnl_checks = 0
    for trade in result.trades:
        quantity = float(trade["quantity"])
        reference = float(trade["reference_price"])
        expected_fill = reference * (
            1 + SLIPPAGE_BPS / 10_000
            if trade["side"] == "BUY"
            else 1 - SLIPPAGE_BPS / 10_000
        )
        if not math.isclose(trade["fill_price"], expected_fill, rel_tol=0, abs_tol=1e-9):
            raise AssertionError(f"滑点成交价错误：{trade}")
        expected_commission = max(
            quantity * COMMISSION_PER_SHARE,
            MINIMUM_COMMISSION,
        )
        if not math.isclose(
            trade["commission"],
            expected_commission,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise AssertionError(f"手续费错误：{trade}")
        gross = quantity * float(trade["fill_price"])
        expected_cash = (
            previous_cash - gross - expected_commission
            if trade["side"] == "BUY"
            else previous_cash + gross - expected_commission
        )
        if not math.isclose(
            trade["cash_after"],
            expected_cash,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise AssertionError(
                f"现金流水错误：expected={expected_cash}, trade={trade}"
            )
        previous_cash = expected_cash
        if trade["side"] == "BUY":
            lots[trade["symbol"]].append(
                [quantity, (gross + expected_commission) / quantity]
            )
        else:
            remaining = quantity
            removed_cost = 0.0
            while remaining > 1e-9:
                lot = lots[trade["symbol"]][0]
                consumed = min(lot[0], remaining)
                removed_cost += consumed * lot[1]
                lot[0] -= consumed
                remaining -= consumed
                if lot[0] <= 1e-9:
                    lots[trade["symbol"]].popleft()
            expected_pnl = gross - expected_commission - removed_cost
            if not math.isclose(
                trade["realized_pnl"],
                expected_pnl,
                rel_tol=0,
                abs_tol=1e-6,
            ):
                raise AssertionError(
                    f"FIFO PnL 错误：expected={expected_pnl}, trade={trade}"
                )
            pnl_checks += 1
        if (
            trade["position_weight_after"]
            > max_weights[trade["symbol"]] / 100 + 1e-8
        ):
            raise AssertionError(f"成交后仓位超过上限：{trade}")

    peak = INITIAL_CAPITAL
    for point in result.equity_points:
        position_value = sum(
            float(position["market_value"])
            for position in point["positions"].values()
        )
        expected_equity = (
            float(point["cash"])
            + float(point.get("receivables", 0))
            + position_value
        )
        if not math.isclose(
            point["equity"],
            expected_equity,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise AssertionError(f"权益恒等式错误：{point}")
        if float(point["cash"]) < -1e-7:
            raise AssertionError(f"出现负现金：{point}")
        expected_return = float(point["equity"]) / INITIAL_CAPITAL - 1
        peak = max(peak, float(point["equity"]))
        expected_drawdown = float(point["equity"]) / peak - 1
        if not math.isclose(
            float(point["return_rate"]),
            expected_return,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise AssertionError(f"累计收益率错误：{point}")
        if not math.isclose(
            float(point["drawdown_rate"]),
            expected_drawdown,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise AssertionError(f"回撤率错误：{point}")
        for symbol, position in point["positions"].items():
            if position["quantity"] < 0:
                raise AssertionError(f"出现负持仓：{point}")
    return {
        "cash_ledger_checks": len(result.trades),
        "fifo_realized_pnl_checks": pnl_checks,
        "equity_identity_checks": len(result.equity_points),
        "slippage_checks": len(result.trades),
        "commission_checks": len(result.trades),
        "return_and_drawdown_checks": len(result.equity_points),
        "benchmark_checks": validate_benchmark(
            result,
            dataset,
            benchmark_weights,
        ),
    }


def result_summary(result, checks: dict) -> dict:
    return {
        "sessions": len(result.equity_points),
        "trades": len(result.trades),
        "first_trade": result.trades[0] if result.trades else None,
        "last_trade": result.trades[-1] if result.trades else None,
        "ending_equity": result.metrics["ending_equity"],
        "total_return": result.metrics["total_return"],
        "max_drawdown": result.metrics["max_drawdown"],
        "total_commission": result.metrics["total_commission"],
        "total_slippage": result.metrics["total_slippage"],
        "checks": checks,
    }


def main() -> None:
    reports = {}

    single = visual_strategy(
        "SPY MA10 单标的",
        "single",
        [{"symbol": "SPY", "max_weight": 100}],
        [
            rule("buy-above-ma10", "BUY", "price > ma(10)", value=100, priority=10),
            rule("sell-below-ma10", "SELL", "price < ma(10)", value=0, priority=20),
        ],
    )
    dataset = load_real_dataset(["SPY"])
    result = BacktestEngine(
        single,
        settings("SPY"),
        dataset=dataset,
    ).run()
    reports["single_ma10"] = result_summary(
        result,
        validate_accounting(
            result,
            dataset,
            max_weights={"SPY": 100},
            benchmark_weights={"SPY": 100},
        ),
    )

    distribution = visual_strategy(
        "SPY GLD 独立分配",
        "distribution",
        [
            {"symbol": "SPY", "max_weight": 50},
            {"symbol": "GLD", "max_weight": 50},
        ],
        [
            rule("buy-above-ma5", "BUY", "price > ma(5)", value=50, priority=10),
            rule("sell-below-ma5", "SELL", "price < ma(5)", value=0, priority=20),
        ],
    )
    dataset = load_real_dataset(["SPY", "GLD"])
    result = BacktestEngine(
        distribution,
        settings("auto"),
        dataset=dataset,
    ).run()
    reports["distribution_ma5"] = result_summary(
        result,
        validate_accounting(
            result,
            dataset,
            max_weights={"SPY": 50, "GLD": 50},
            benchmark_weights={"SPY": 50, "GLD": 50},
        ),
    )

    competition = visual_strategy(
        "五标的 ATR 竞争",
        "competition",
        [{"symbol": symbol, "max_weight": 100} for symbol in UNIVERSE],
        [
            rule(
                "risk-exit",
                "SELL",
                "price < ma(10)",
                value=0,
                priority=10,
            )
        ],
        competition={
            "eligibility": "price > ma(5)",
            "score": "(price - close(5)) / atr(5)",
            "target_weight": 100,
            "cash_when_none": True,
            "when": "OPEN",
        },
    )
    dataset = load_real_dataset(UNIVERSE)
    result = BacktestEngine(
        competition,
        settings("auto"),
        dataset=dataset,
    ).run()
    if any(len(point["positions"]) > 1 for point in result.equity_points):
        raise AssertionError("competition 模式同时持有了多个标的。")
    reports["competition_atr"] = result_summary(
        result,
        validate_accounting(
            result,
            dataset,
            max_weights={symbol: 100 for symbol in UNIVERSE},
            benchmark_weights={symbol: 20 for symbol in UNIVERSE},
        ),
    )

    code = {
        "name": "急跌回避与 ATR 动量轮动",
        "design_mode": "code",
        "selection_mode": "competition",
        "code_key": "rapid_drop_atr_rotation",
        "code_version": "1.3.0",
        "definition": {
            "symbols": [
                {"symbol": symbol, "max_weight": 100}
                for symbol in UNIVERSE
            ],
            "params": {},
        },
        "default_settings": settings(),
    }
    dataset = load_real_dataset(
        UNIVERSE,
        intraday_events=["09:40", "10:00"],
    )
    result = BacktestEngine(
        code,
        settings("auto"),
        dataset=dataset,
    ).run()
    if any(len(point["positions"]) > 1 for point in result.equity_points):
        raise AssertionError("代码轮动策略同时持有了多个标的。")
    if any(
        trade["side"] == "BUY" and "10:00" not in trade["event_time"]
        for trade in result.trades
    ):
        raise AssertionError("代码策略在非 10:00 时刻买入。")
    reports["code_rapid_drop_atr_rotation"] = result_summary(
        result,
        validate_accounting(
            result,
            dataset,
            max_weights={symbol: 100 for symbol in UNIVERSE},
            benchmark_weights={symbol: 20 for symbol in UNIVERSE},
        ),
    )

    print(
        json.dumps(
            {
                "ok": True,
                "window": [START_DATE, END_DATE],
                "commission_per_share": COMMISSION_PER_SHARE,
                "minimum_commission": MINIMUM_COMMISSION,
                "slippage_bps": SLIPPAGE_BPS,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
