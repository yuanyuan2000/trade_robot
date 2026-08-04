from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from database import backtest_repository, repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.data import EventPrice, HistoricalDataSet
from services.backtest.engine import BacktestEngine
from services.backtest.portfolio import D, Lot, Portfolio, Position
from services.backtest.validation import validate_strategy_payload
from services.realtime_market_data import IEXMarketDataHub


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def _json_safe(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _strategy_state(strategy_instance) -> dict:
    return _json_safe(getattr(strategy_instance, "__dict__", {}))


def _restore_strategy_state(strategy_instance, state: dict | None) -> None:
    if not state:
        return
    for key, value in state.items():
        if key in {"risk_off"}:
            value = {str(day): set(items) for day, items in value.items()}
        setattr(strategy_instance, key, value)


def _restore_portfolio(portfolio: Portfolio, state: dict | None) -> None:
    if not state:
        return
    portfolio.cash = D(state.get("cash", float(portfolio.initial_cash)))
    for symbol, item in (state.get("positions") or {}).items():
        quantity = D(item.get("quantity", 0))
        if quantity <= 0:
            continue
        average_cost = D(item.get("average_cost", item.get("price", 0)))
        position = Position(symbol=symbol, quantity=quantity)
        position.lots = [Lot(quantity=quantity, unit_cost=average_cost)]
        portfolio.positions[symbol] = position
    portfolio.realized_pnl = D(state.get("realized_pnl", 0))


def _portfolio_state(portfolio: Portfolio, marks: dict[str, float]) -> dict:
    return {
        "cash": float(portfolio.cash),
        "realized_pnl": float(portfolio.realized_pnl),
        "positions": portfolio.snapshot(marks),
    }


def _current_daily_row(symbol: str, payload: dict) -> dict:
    daily = payload.get("daily") or {}
    daily_is_complete_bar = bool(
        payload.get("daily_is_complete")
        and daily.get("open") is not None
    )
    source = daily if daily_is_complete_bar else payload.get("latest_minute") or daily or {}
    return {
        "date": payload.get("trading_date"),
        "open": float(source.get("open", payload["signal_price"])),
        "high": float(source.get("high", payload["signal_price"])),
        "low": float(source.get("low", payload["signal_price"])),
        "close": float(source.get("close", payload["signal_price"])),
        "volume": float(source.get("volume") or 0),
        "source_provider": "alpaca",
        "source_timeframe": "1Day" if daily_is_complete_bar else "1Min",
        "price_basis": "raw",
        "is_complete": 0 if payload.get("event") not in {"CLOSE"} else 1,
    }


def _build_dataset(strategy: dict, payload: dict, trading_date: str) -> HistoricalDataSet:
    definition = strategy["definition"]
    symbols = [str(item["symbol"]).upper() for item in definition.get("symbols", [])]
    if strategy["design_mode"] == "code":
        strategy_type = get_code_strategy(strategy["code_key"])
        params = strategy_type.validate_params(definition.get("params", {}))
        symbols.extend(strategy_type.additional_symbols(params))
    symbols = list(dict.fromkeys(symbols))
    daily: dict[str, list[dict]] = {}
    for symbol in symbols:
        rows = repository.get_daily_prices(symbol, include_metadata=True)
        if not rows:
            raise RuntimeError(f"{symbol} 没有本地日线数据，无法进行实时决策。")
        daily[symbol] = [dict(row) for row in rows]
        live_payload = payload["symbols"].get(symbol)
        if not live_payload:
            raise RuntimeError(f"{symbol} 缺少 IEX 实时行情。")
        daily[symbol] = [row for row in daily[symbol] if row.get("date") != trading_date]
        daily[symbol].append({**_current_daily_row(symbol, live_payload), "date": trading_date})
    actions = backtest_repository.get_corporate_actions(
        symbols,
        start_date=(date.fromisoformat(trading_date) - timedelta(days=370)).isoformat(),
        end_date=trading_date,
    )
    market_session = {
        "trading_date": trading_date,
        "open_minute_utc": int(datetime.fromisoformat(f"{trading_date}T09:30:00").replace(tzinfo=NEW_YORK).astimezone(UTC).timestamp()) // 60,
        "close_minute_utc": int(datetime.fromisoformat(f"{trading_date}T16:00:00").replace(tzinfo=NEW_YORK).astimezone(UTC).timestamp()) // 60,
        "is_early_close": False,
    }
    cumulative: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        volume = payload["symbols"][symbol].get("cumulative_volume")
        if volume is not None:
            cumulative[symbol] = {f"{trading_date}|{payload['event']}": float(volume)}
    manifest = {
        "data_contract_version": 1,
        "source": "alpaca",
        "feed": "iex",
        "symbols": {symbol: {"daily_rows": len(daily[symbol])} for symbol in symbols},
        "market_sessions": [market_session],
        "timezone": "America/New_York",
        "trading_date": trading_date,
    }
    return HistoricalDataSet(
        daily=daily,
        sessions=[trading_date],
        cumulative_volumes=cumulative,
        availability_start={symbol: daily[symbol][0]["date"] for symbol in symbols},
        corporate_actions=actions,
        manifest=manifest,
    )


class RealtimeDecisionEvaluator:
    """Run one live event using the same strategy evaluator as backtests."""

    def __init__(self, hub: IEXMarketDataHub):
        self.hub = hub

    def evaluate(self, task: dict, run: dict, *, trading_date: str, event: str) -> dict:
        strategy = validate_strategy_payload(deepcopy(run["strategy_snapshot"]))
        definition = strategy["definition"]
        candidate_symbols = [str(item["symbol"]).upper() for item in definition.get("symbols", [])]
        symbols = list(candidate_symbols)
        include_volume = False
        auxiliary_symbols: list[str] = []
        if strategy["design_mode"] == "code":
            strategy_type = get_code_strategy(strategy["code_key"])
            params = strategy_type.validate_params(definition.get("params", {}))
            auxiliary_symbols = list(strategy_type.additional_symbols(params))
            symbols.extend(auxiliary_symbols)
            include_volume = event in strategy_type.cumulative_volume_events(params)
        symbols = list(dict.fromkeys(symbols))
        allow_partial = strategy["selection_mode"] == "competition"
        payload = self.hub.event_snapshot(
            symbols,
            trading_date=trading_date,
            event=event,
            include_cumulative_volume=include_volume,
            allow_missing=allow_partial,
        )
        payload["trading_date"] = trading_date
        payload["event"] = event
        available = set(payload["symbols"])
        missing_auxiliary = [symbol for symbol in auxiliary_symbols if symbol not in available]
        if missing_auxiliary:
            raise RuntimeError("代码策略所需辅助标的行情缺失：" + "、".join(missing_auxiliary))
        if allow_partial:
            available_candidates = [symbol for symbol in candidate_symbols if symbol in available]
            if len(available_candidates) < 2:
                raise RuntimeError("competition 模式至少需要两个标的取得有效 IEX 行情。")
            strategy["definition"]["symbols"] = [
                item for item in definition.get("symbols", [])
                if str(item["symbol"]).upper() in set(available_candidates)
            ]
            definition = strategy["definition"]
        dataset = _build_dataset(strategy, payload, trading_date)
        settings = {
            **strategy.get("default_settings", {}),
            **(run.get("settings") or {}),
            "start_date": trading_date,
            "end_date": trading_date,
            "benchmark": "none",
            "strict_data": True,
        }
        engine = BacktestEngine(strategy, settings, dataset=dataset)
        _restore_strategy_state(engine.code_strategy, (run.get("state") or {}).get("strategy_state"))
        _restore_portfolio(engine.portfolio, (run.get("state") or {}).get("portfolio"))
        event_prices: dict[str, EventPrice] = {}
        for symbol, item in payload["symbols"].items():
            event_prices[symbol] = EventPrice(
                signal_price=float(item["signal_price"]),
                fill_price=float(item["fill_price"]) if item.get("fill_price") is not None else None,
                signal_time=item.get("signal_time") or f"{trading_date} {event}",
                fill_time=item.get("fill_time"),
            )
        intents = engine._strategy_intents(
            trading_date=trading_date,
            event=event,
            event_prices=event_prices,
        )
        marks = {symbol: value.signal_price for symbol, value in event_prices.items()}
        trades = []
        if event != "CLOSE" and all(
            value.fill_price is not None for value in event_prices.values()
            if value.signal_price is not None
        ):
            engine._execute_intents(
                intents,
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
            )
            trades = list(engine.trades)
        recommendations = []
        for intent in intents:
            item = next((entry for entry in definition.get("symbols", []) if entry["symbol"] == intent.symbol), {})
            symbol_leverage = float(item.get("leverage_multiplier", 1))
            recommendations.append({
                "symbol": intent.symbol,
                "action": intent.action,
                "target_weight_percent": float(intent.value_percent),
                "effective_leverage": float(settings.get("leverage_multiplier", 1)) * symbol_leverage,
                "reason": intent.reason,
            })
        decision = {
            "task_id": task["id"],
            "run_id": run["id"],
            "trading_date": trading_date,
            "event": event,
            "source": "alpaca",
            "feed": "iex",
            "recommendations": recommendations,
            "orders": [intent.__dict__ for intent in intents],
            "trades": trades,
            "portfolio": _portfolio_state(engine.portfolio, marks),
            "data_warnings": list(payload.get("missing") or []),
        }
        calculation = {
            "engine_logs": engine.logs,
            "symbol_inputs": {
                symbol: {
                    "signal_price": value["signal_price"],
                    "signal_time": value["signal_time"],
                    "latest_minute": value.get("latest_minute"),
                    "cumulative_volume": value.get("cumulative_volume"),
                }
                for symbol, value in payload["symbols"].items()
            },
            "strategy_state": _strategy_state(engine.code_strategy) if engine.code_strategy is not None else {},
        }
        state = {
            "strategy_state": calculation["strategy_state"],
            "portfolio": decision["portfolio"],
        }
        return {
            "data_manifest": payload,
            "decision": decision,
            "calculation": calculation,
            "state": state,
        }
