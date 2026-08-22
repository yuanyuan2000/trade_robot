from __future__ import annotations

import hashlib
from typing import Callable

from services.backtest.code_strategies import get_code_strategy
from services.backtest.dsl import compile_expression
from services.market_data_integrity import (
    MarketDataIntegrityError,
    assess_daily_history,
    frozen_daily_rows,
    required_completed_sessions,
)


def strategy_history_requirements(strategy: dict) -> tuple[list[str], int]:
    definition = strategy["definition"]
    candidates = [
        str(item["symbol"]).strip().upper()
        for item in definition.get("symbols", [])
    ]
    symbols = list(candidates)
    minimum = 2
    if strategy["design_mode"] == "visual":
        expressions = [
            str(rule.get("condition") or "true")
            for rule in definition.get("rules", [])
            if rule.get("enabled", True)
        ]
        if strategy["selection_mode"] == "competition":
            competition = definition["competition"]
            expressions.extend((competition["eligibility"], competition["score"]))
        minimum = max(
            minimum,
            max(
                (compile_expression(value).max_lookback for value in expressions),
                default=1,
            ) + 1,
        )
    else:
        strategy_type = get_code_strategy(strategy["code_key"])
        params = strategy_type.validate_params(definition.get("params", {}))
        symbols.extend(strategy_type.additional_symbols(params))
        minimum = max(minimum, int(strategy_type.minimum_lookback(params)) + 1)
    return list(dict.fromkeys(symbols)), minimum


def prepare_strategy_history(
    strategy: dict,
    *,
    trading_date: str,
    refresh: Callable[[str, str], object] | None = None,
) -> dict:
    symbols, minimum = strategy_history_requirements(strategy)
    market = strategy.get("market")
    expected_dates = required_completed_sessions(trading_date, minimum)
    audits: dict[str, dict] = {}
    histories: dict[str, list[dict]] = {}
    failures: list[str] = []
    for symbol in symbols:
        audit = assess_daily_history(symbol, expected_dates, market=market)
        if not audit["complete"] and refresh is not None:
            refresh(symbol, audit["repair_start_date"] or expected_dates[0])
            audit = assess_daily_history(symbol, expected_dates, market=market)
        audits[symbol] = audit
        if not audit["complete"]:
            failures.append(
                f"{symbol}: 截止 {audit['latest_complete_date'] or '无'}，"
                f"缺失 {','.join(audit['missing_sessions'] + audit['incomplete_sessions']) or '所需历史'}"
            )
            continue
        histories[symbol] = frozen_daily_rows(
            symbol,
            before_date=trading_date,
            market=market,
        )
    if failures:
        raise MarketDataIntegrityError("实时决策历史数据不完整：" + "；".join(failures))
    snapshot_ids = sorted(audit["snapshot_id"] for audit in audits.values())
    combined_fingerprint = hashlib.sha256(
        "|".join(snapshot_ids).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "required_sessions": minimum,
        "expected_dates": expected_dates,
        "symbols": audits,
        "daily": histories,
        "snapshot_id": f"history:{combined_fingerprint}",
        "market": market,
    }
