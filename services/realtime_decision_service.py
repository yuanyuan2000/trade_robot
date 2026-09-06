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
from services.backtest.portfolio import D, Lot, OrderIntent, Portfolio, Position
from services.backtest.validation import validate_strategy_payload
from services.realtime_market_data import IEXMarketDataHub
from services.market_data_request_coordinator import PRIORITY_FORMAL_DECISION
from services.market_data_service import refresh_strategy_daily_history
from services.market_context import market_sessions
from services.realtime_config import (
    REALTIME_MODEL_NOTIONAL,
    normalize_recommendation_state,
    realtime_engine_settings,
)
from services.realtime_history_service import prepare_strategy_history


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
    # Parameters belong to the immutable strategy snapshot, not runtime state.
    # Keeping them in state would let an older event overwrite newly followed
    # parameters after a code-strategy revision.
    return _json_safe({
        key: value
        for key, value in getattr(strategy_instance, "__dict__", {}).items()
        if key != "params"
    })


def _restore_strategy_state(strategy_instance, state: dict | None) -> None:
    if not state:
        return
    for key, value in state.items():
        # Only restore fields declared by the currently constructed strategy.
        # This makes added/removed runtime fields safe across followed revisions
        # and never permits persisted state to replace validated parameters.
        if key == "params" or not hasattr(strategy_instance, key):
            continue
        if key in {"risk_off"}:
            value = {str(day): set(items) for day, items in value.items()}
        setattr(strategy_instance, key, value)


def _restore_portfolio(portfolio: Portfolio, state: dict | None) -> None:
    """Compatibility helper for legacy leverage state.

    New realtime runs use `_restore_recommendation_state`; this narrow helper
    remains for old snapshots and callers that only need leverage migration.
    """
    normalized = normalize_recommendation_state(state)
    configured = normalized["configured_symbol_leverage_multipliers"]
    effective = normalized["symbol_leverage_multipliers"]
    for symbol, multiplier in configured.items():
        portfolio.set_configured_symbol_leverage_multiplier(symbol, float(multiplier))
    for symbol, multiplier in effective.items():
        portfolio.set_symbol_leverage_multiplier(symbol, float(multiplier))


def _restore_recommendation_state(
    portfolio: Portfolio,
    state: dict | None,
    marks: dict[str, float],
) -> dict:
    """Rehydrate a scale-neutral portfolio view for legacy strategy code.

    Realtime tasks persist percentages only. The shared strategy evaluator still
    exposes Portfolio methods to built-in strategies, so this adapter creates
    fractional model quantities at the current marks. No cash, fills or P&L are
    carried between events.
    """
    normalized = normalize_recommendation_state(state)
    configured = normalized["configured_symbol_leverage_multipliers"]
    effective = normalized["symbol_leverage_multipliers"]
    for symbol, multiplier in configured.items():
        portfolio.set_configured_symbol_leverage_multiplier(symbol, float(multiplier))
    for symbol, multiplier in effective.items():
        portfolio.set_symbol_leverage_multiplier(symbol, float(multiplier))
    portfolio.strategy_target_weights = {
        str(symbol): D(float(percent) / 100.0)
        for symbol, percent in normalized["recommended_targets"].items()
        if float(percent) > 0
    }
    portfolio.positions.clear()
    portfolio.cash = D(REALTIME_MODEL_NOTIONAL)
    exposures = dict(normalized["recommended_exposures"])
    for symbol, target_percent in normalized["recommended_targets"].items():
        if symbol not in exposures:
            leverage = float(portfolio.effective_leverage(symbol))
            exposures[symbol] = float(target_percent) * leverage
    for symbol, exposure_percent in exposures.items():
        price = float(marks.get(symbol) or 0)
        if price <= 0 or float(exposure_percent) <= 0:
            continue
        market_value = REALTIME_MODEL_NOTIONAL * float(exposure_percent) / 100.0
        quantity = D(market_value / price)
        position = Position(symbol=symbol, quantity=quantity)
        position.lots.append(Lot(quantity=quantity, unit_cost=D(price)))
        position.cost_basis_value = D(market_value)
        portfolio.positions[symbol] = position
        portfolio.cash -= D(market_value)
    return normalized


def _recommendation_state(
    portfolio: Portfolio,
    exposures: dict[str, float],
) -> dict:
    targets = {
        str(symbol): float(weight) * 100.0
        for symbol, weight in portfolio.strategy_target_weights.items()
        if float(weight) > 1e-12
    }
    normalized_exposures = {
        str(symbol): float(percent)
        for symbol, percent in exposures.items()
        if float(percent) > 1e-12
    }
    return {
        "state_version": 2,
        "recommended_targets": targets,
        "recommended_exposures": normalized_exposures,
        "configured_symbol_leverage_multipliers": {
            str(symbol): float(value)
            for symbol, value in portfolio.configured_symbol_leverage_multipliers.items()
        },
        "symbol_leverage_multipliers": {
            str(symbol): float(value)
            for symbol, value in portfolio.symbol_leverage_multipliers.items()
        },
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
        "source_provider": payload.get("source") or (
            "alpaca_crypto" if "/" in symbol else "alpaca"
        ),
        "source_timeframe": (
            "1Day"
            if daily_is_complete_bar
            else "CurrentPrice"
            if payload.get("price_fallback") == "current_price_without_alpaca_minutes"
            else "1Min"
        ),
        "price_basis": "raw",
        "is_complete": 0 if payload.get("event") not in {"CLOSE"} else 1,
    }


def _build_dataset(
    strategy: dict,
    payload: dict,
    trading_date: str,
    *,
    history_daily: dict[str, list[dict]] | None = None,
    market_session: dict | None = None,
    runtime_symbols: list[str] | None = None,
) -> HistoricalDataSet:
    definition = strategy["definition"]
    symbols = [str(item["symbol"]).upper() for item in definition.get("symbols", [])]
    if strategy["design_mode"] == "code":
        strategy_type = get_code_strategy(strategy["code_key"])
        params = strategy_type.validate_params(definition.get("params", {}))
        symbols.extend(strategy_type.additional_symbols(params))
    symbols.extend(str(symbol).upper() for symbol in (runtime_symbols or []))
    symbols = list(dict.fromkeys(symbols))
    daily: dict[str, list[dict]] = {}
    for symbol in symbols:
        rows = (
            [dict(row) for row in history_daily[symbol]]
            if history_daily is not None and symbol in history_daily
            else repository.get_daily_prices(symbol, include_metadata=True)
        )
        live_payload = payload["symbols"].get(symbol)
        if not live_payload:
            raise RuntimeError(f"{symbol} 缺少正式事件实时行情。")
        if not rows:
            rows = [_current_daily_row(symbol, live_payload)]
        daily[symbol] = [dict(row) for row in rows]
        daily[symbol] = [row for row in daily[symbol] if row.get("date") != trading_date]
        daily[symbol].append({**_current_daily_row(symbol, live_payload), "date": trading_date})
    actions = backtest_repository.get_corporate_actions(
        symbols,
        start_date=(date.fromisoformat(trading_date) - timedelta(days=370)).isoformat(),
        end_date=trading_date,
    )
    market_session = dict(market_session or {
        "trading_date": trading_date,
        "open_minute_utc": int(datetime.fromisoformat(f"{trading_date}T09:30:00").replace(tzinfo=NEW_YORK).astimezone(UTC).timestamp()) // 60,
        "close_minute_utc": int(datetime.fromisoformat(f"{trading_date}T16:00:00").replace(tzinfo=NEW_YORK).astimezone(UTC).timestamp()) // 60,
        "is_early_close": False,
    })
    cumulative: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        volume = payload["symbols"][symbol].get("cumulative_volume")
        if volume is not None:
            cumulative[symbol] = {f"{trading_date}|{payload['event']}": float(volume)}
    manifest = {
        "data_contract_version": 3,
        "source": payload.get("source") or "alpaca",
        "feed": payload.get("feed") or "iex",
        "market": strategy.get("market"),
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

    def evaluate(
        self,
        task: dict,
        run: dict,
        *,
        trading_date: str,
        event: str,
        scheduled_at: datetime | None = None,
        prepared_observation: dict | None = None,
    ) -> dict:
        strategy = validate_strategy_payload(deepcopy(run["strategy_snapshot"]))
        definition = strategy["definition"]
        candidate_symbols = [str(item["symbol"]).upper() for item in definition.get("symbols", [])]
        symbols = list(candidate_symbols)
        saved_run_state = run.get("state") or {}
        restored_portfolio_state = saved_run_state.get("portfolio") or {}
        restored_recommendations = normalize_recommendation_state(
            restored_portfolio_state
        )
        # A followed revision may remove a previously recommended symbol. It
        # remains observable until the model publishes its zero target.
        symbols.extend(
            str(symbol).upper()
            for symbol in restored_recommendations["recommended_targets"]
        )
        include_volume = False
        auxiliary_symbols: list[str] = []
        if strategy["design_mode"] == "code":
            strategy_type = get_code_strategy(strategy["code_key"])
            params = strategy_type.validate_params(definition.get("params", {}))
            auxiliary_symbols = list(strategy_type.additional_symbols(params))
            symbols.extend(auxiliary_symbols)
            include_volume = event in strategy_type.cumulative_volume_events(params)
        symbols = list(dict.fromkeys(symbols))
        effective_settings = {
            **strategy.get("default_settings", {}),
            **(run.get("settings") or {}),
        }
        if prepared_observation is None:
            history_snapshot = prepare_strategy_history(
                strategy,
                trading_date=trading_date,
                settings=effective_settings,
                refresh=lambda symbol, start_date: refresh_strategy_daily_history(
                    symbol,
                    start_date=start_date,
                    market_type=strategy["market"]["type"],
                    priority=PRIORITY_FORMAL_DECISION,
                ),
            )
            session_rows = market_sessions(
                trading_date,
                trading_date,
                strategy["market"],
            )
            if not session_rows:
                raise RuntimeError(f"{trading_date} 不是策略市场交易日。")
            market_session = session_rows[0]
        else:
            if str(prepared_observation.get("trading_date")) != trading_date:
                raise ValueError("本地测试快照日期与决策日期不一致。")
            history_snapshot = deepcopy(prepared_observation["history_snapshot"])
            market_session = deepcopy(prepared_observation["market_session"])
        effective_target_at = scheduled_at
        if effective_target_at is None and event not in {"OPEN", "CLOSE"}:
            requested = datetime.fromisoformat(
                f"{trading_date}T{event}:00"
            ).replace(tzinfo=NEW_YORK).astimezone(UTC)
            open_at = datetime.fromtimestamp(
                int(market_session["open_minute_utc"]) * 60,
                tz=UTC,
            )
            close_at = datetime.fromtimestamp(
                int(market_session["close_minute_utc"]) * 60,
                tz=UTC,
            )
            effective_target_at = requested
            if not open_at <= requested < close_at:
                offset = None
                if strategy["design_mode"] == "code" and market_session.get("is_early_close"):
                    strategy_type = get_code_strategy(strategy["code_key"])
                    offset = strategy_type.early_close_offsets(
                        definition.get("params", {})
                    ).get(event)
                if offset is not None:
                    effective_target_at = close_at - timedelta(minutes=int(offset))
        allow_partial = strategy["selection_mode"] == "competition"
        previous_session_closes = {
            symbol: {
                "date": rows[-1]["date"],
                "close": rows[-1]["close"],
            }
            for symbol, rows in history_snapshot["daily"].items()
            if rows
        }
        if prepared_observation is None:
            payload = self.hub.event_snapshot(
                symbols,
                trading_date=trading_date,
                event=event,
                include_cumulative_volume=include_volume,
                allow_missing=allow_partial,
                market_session=market_session,
                effective_target_at=effective_target_at,
                previous_session_closes=previous_session_closes,
            )
        else:
            payload = deepcopy(prepared_observation["payload"])
            payload["event"] = event
            requested_symbols = set(symbols)
            payload["symbols"] = {
                symbol: item
                for symbol, item in payload.get("symbols", {}).items()
                if symbol in requested_symbols
            }
        payload["trading_date"] = trading_date
        payload["event"] = event
        available = set(payload["symbols"])
        missing_auxiliary = [symbol for symbol in auxiliary_symbols if symbol not in available]
        if missing_auxiliary:
            raise RuntimeError("代码策略所需辅助标的行情缺失：" + "、".join(missing_auxiliary))
        if allow_partial:
            required_candidates = list(candidate_symbols)
            missing_candidates = [
                symbol for symbol in required_candidates if symbol not in available
            ]
            if missing_candidates:
                raise RuntimeError(
                    "正式候选池实时行情缺失：" + "、".join(missing_candidates)
                )
            available_candidates = [
                symbol for symbol in candidate_symbols if symbol in available
            ]
            if len(available_candidates) < 2:
                raise RuntimeError("competition 模式至少需要两个标的取得有效正式事件行情。")
            strategy["definition"]["symbols"] = [
                item for item in definition.get("symbols", [])
                if str(item["symbol"]).upper() in set(available_candidates)
            ]
            definition = strategy["definition"]
        dataset = _build_dataset(
            strategy,
            payload,
            trading_date,
            history_daily=history_snapshot["daily"],
            market_session=market_session,
            runtime_symbols=list(restored_recommendations["recommended_targets"]),
        )
        settings = realtime_engine_settings(effective_settings, trading_date)
        engine = BacktestEngine(strategy, settings, dataset=dataset)
        held_runtime_symbols = list(
            restored_recommendations["recommended_targets"].keys()
        )
        for symbol in held_runtime_symbols:
            if symbol not in engine.tradable_symbols:
                engine.tradable_symbols.append(symbol)
                engine.max_weights[symbol] = 100.0
                engine.symbol_leverages.setdefault(symbol, 1.0)
        saved_strategy_state = saved_run_state.get("strategy_state")
        if engine.code_strategy is not None:
            _restore_strategy_state(engine.code_strategy, saved_strategy_state)
        else:
            engine.restore_visual_strategy_state(saved_strategy_state)
        event_prices: dict[str, EventPrice] = {}
        for symbol, item in payload["symbols"].items():
            event_prices[symbol] = EventPrice(
                signal_price=float(item["signal_price"]),
                fill_price=float(item["fill_price"]) if item.get("fill_price") is not None else None,
                signal_time=item.get("signal_time") or f"{trading_date} {event}",
                fill_time=item.get("fill_time"),
            )
        marks = {symbol: value.signal_price for symbol, value in event_prices.items()}
        restored_recommendations = _restore_recommendation_state(
            engine.portfolio,
            restored_portfolio_state,
            marks,
        )

        pending_close = [
            OrderIntent(**item)
            for item in (saved_run_state.get("pending_close_orders") or [])
        ]
        if event == "OPEN" and pending_close:
            engine.execute_event_intents(
                pending_close,
                trading_date=trading_date,
                event="OPEN",
                event_prices=event_prices,
                reason_prefix="前一交易日收盘信号",
            )
            pending_close = []

        intents = engine.strategy_intents_for_event(
            trading_date=trading_date,
            event=event,
            event_prices=event_prices,
        )
        # A followed revision can remove a previous model target. Publish an
        # explicit zero target so the user is never left with an orphaned idea.
        for symbol in held_runtime_symbols:
            if (
                symbol not in engine.universe
                and symbol not in engine.auxiliary_symbols
                and engine.portfolio.quantity(symbol) > 0
                and not any(intent.symbol == symbol for intent in intents)
            ):
                intents.append(OrderIntent(
                    symbol=symbol,
                    action="SELL",
                    sizing_mode="TARGET",
                    value_percent=0,
                    reason="跟随源策略更新：标的已移出策略范围",
                ))
        if event == "CLOSE":
            pending_close = list(intents)
        elif all(
            value.fill_price is not None for value in event_prices.values()
            if value.signal_price is not None
        ):
            engine.execute_event_intents(
                intents,
                trading_date=trading_date,
                event=event,
                event_prices=event_prices,
            )
        recommendations = []
        for intent in intents:
            recommendations.append({
                "symbol": intent.symbol,
                "action": intent.action,
                "target_weight_percent": float(intent.value_percent),
                "effective_leverage": float(
                    engine.portfolio.effective_leverage(intent.symbol)
                ),
                "target_exposure_percent": float(
                    engine.last_decision_exposures.get(intent.symbol, 0.0)
                ),
                "reason": intent.reason,
            })
        recommendation_state = _recommendation_state(
            engine.portfolio,
            engine.last_decision_exposures,
        )
        persisted_recommendation_state = recommendation_state
        if event == "CLOSE":
            # CLOSE publishes a prospective target but remains pending until
            # the next OPEN. Preserve active exposure separately so the next
            # event can apply the transition without assuming an early fill.
            persisted_recommendation_state = {
                **recommendation_state,
                "recommended_exposures": dict(
                    restored_recommendations["recommended_exposures"]
                ),
            }
        target_symbols = set(restored_recommendations["recommended_targets"]) | set(
            recommendation_state["recommended_targets"]
        )
        target_portfolio = []
        for symbol in sorted(target_symbols):
            previous = float(
                restored_recommendations["recommended_targets"].get(symbol, 0)
            )
            target = float(recommendation_state["recommended_targets"].get(symbol, 0))
            exposure = float(
                recommendation_state["recommended_exposures"].get(symbol, 0)
            )
            change = target - previous
            target_portfolio.append({
                "symbol": symbol,
                "previous_target_weight_percent": previous,
                "target_weight_percent": target,
                "target_exposure_percent": exposure,
                "change_percent_points": change,
                "change": (
                    "ENTER" if previous <= 0 < target
                    else "EXIT" if previous > 0 >= target
                    else "ADJUST" if abs(change) > 1e-10
                    else "HOLD"
                ),
            })
        decision = {
            "task_id": task["id"],
            "run_id": run["id"],
            "trading_date": trading_date,
            "event": event,
            "source": payload.get("source") or "alpaca",
            "feed": payload.get("feed") or "iex",
            "recommendations": recommendations,
            "target_portfolio": target_portfolio,
            "orders": [intent.__dict__ for intent in intents],
            "trades": [],
            "data_warnings": list(payload.get("missing") or []),
        }
        audit_logs = []
        for symbol, audit in history_snapshot["symbols"].items():
            audit_logs.append({
                "level": "INFO",
                "event_type": "DATA_HISTORY_AUDIT",
                "message": (
                    f"{symbol} 历史数据截至 {audit.get('latest_complete_date')}，"
                    f"快照 {audit.get('snapshot_id')}。"
                ),
                "symbol": symbol,
                "context": audit,
            })
        event_snapshot_id = (
            f"{history_snapshot['snapshot_id']}|"
            f"{payload.get('feed')}:{payload.get('requested_at')}"
        )
        audit_logs.append({
            "level": "INFO",
            "event_type": "DATA_EVENT_SNAPSHOT",
            "message": f"正式事件行情快照 {event_snapshot_id}。",
            "symbol": None,
            "context": {
                "snapshot_id": event_snapshot_id,
                "source": payload.get("source"),
                "feed": payload.get("feed"),
                "requested_at": payload.get("requested_at"),
                "signals": {
                    symbol: {
                        "signal_price": item.get("signal_price"),
                        "signal_time": item.get("signal_time"),
                    }
                    for symbol, item in payload["symbols"].items()
                },
            },
        })
        history_manifest = {
            key: value
            for key, value in history_snapshot.items()
            if key != "daily"
        }
        calculation = {
            "engine_logs": [*engine.logs, *audit_logs],
            "history_snapshot": history_manifest,
            "event_snapshot_id": event_snapshot_id,
            "symbol_inputs": {
                symbol: {
                    "signal_price": value["signal_price"],
                    "signal_time": value["signal_time"],
                    "latest_minute": value.get("latest_minute"),
                    "cumulative_volume": value.get("cumulative_volume"),
                }
                for symbol, value in payload["symbols"].items()
            },
            "strategy_state": (
                _strategy_state(engine.code_strategy)
                if engine.code_strategy is not None
                else engine.visual_strategy_state()
            ),
        }
        state = {
            "strategy_state": calculation["strategy_state"],
            "portfolio": persisted_recommendation_state,
            "pending_close_orders": [intent.__dict__ for intent in pending_close],
        }
        return {
            "data_manifest": payload,
            "decision": decision,
            "calculation": calculation,
            "state": state,
        }
