from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time

from database import backtest_repository
from services.backtest.code_strategies import (
    get_code_strategy,
    list_code_strategies,
)
from services.backtest.engine import BacktestEngine
from services.backtest.errors import (
    BacktestCancelled,
    BacktestError,
    BacktestValidationError,
)
from services.backtest.validation import (
    default_strategy_payload,
    validate_settings,
    validate_strategy_payload,
)


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _validate_code_configuration(strategy: dict) -> dict:
    if strategy["design_mode"] != "code":
        return strategy
    strategy_type = get_code_strategy(strategy["code_key"])
    if strategy["selection_mode"] not in strategy_type.selection_modes:
        raise BacktestValidationError(
            f"代码策略 {strategy_type.name} 不支持 {strategy['selection_mode']} 模式。"
        )
    strategy["definition"]["params"] = strategy_type.validate_params(
        strategy["definition"].get("params", {})
    )
    strategy_type.validate_definition(strategy["definition"])
    strategy["code_version"] = strategy_type.version
    return strategy


def create_strategy(payload: dict) -> dict:
    value = deepcopy(payload)
    if value.get("design_mode") == "code" and value.get("code_key"):
        strategy_type = get_code_strategy(value["code_key"])
        definition = value.setdefault("definition", {})
        definition.setdefault("symbols", deepcopy(strategy_type.default_symbols))
        definition.setdefault("params", {})
        value.setdefault("selection_mode", strategy_type.selection_modes[0])
        value["code_version"] = strategy_type.version
    validated = _validate_code_configuration(
        validate_strategy_payload(value, creating=True)
    )
    return backtest_repository.create_strategy(validated)


def create_default_strategy(
    *,
    name: str,
    design_mode: str,
    selection_mode: str,
    code_key: str | None = None,
) -> dict:
    if design_mode == "code":
        strategy_type = get_code_strategy(code_key or "")
        payload = {
            "name": name,
            "design_mode": "code",
            "selection_mode": selection_mode,
            "code_key": strategy_type.key,
            "code_version": strategy_type.version,
            "definition": {
                "symbols": deepcopy(strategy_type.default_symbols),
                "params": {},
            },
            "default_settings": {},
        }
    else:
        payload = default_strategy_payload(
            name=name,
            design_mode=design_mode,
            selection_mode=selection_mode,
        )
    return create_strategy(payload)


def update_strategy(strategy_id: int, payload: dict) -> dict:
    current = backtest_repository.get_strategy(strategy_id)
    merged = {
        **current,
        **payload,
        "definition": payload.get("definition", current["definition"]),
        "default_settings": payload.get(
            "default_settings",
            current["default_settings"],
        ),
    }
    validated = _validate_code_configuration(validate_strategy_payload(merged))
    return backtest_repository.update_strategy(
        strategy_id,
        validated,
        expected_revision=payload.get("revision"),
    )


def duplicate_strategy(strategy_id: int, name: str | None = None) -> dict:
    current = backtest_repository.get_strategy(strategy_id)
    if current["design_mode"] == "code":
        raise BacktestValidationError("内置代码策略不能在前端复制或新建。")
    base_name = str(name or f"{current['name']} 副本").strip()
    candidate = base_name
    existing = {item["name"].casefold() for item in backtest_repository.list_strategies()}
    suffix = 2
    while candidate.casefold() in existing:
        candidate = f"{base_name} {suffix}"
        suffix += 1
    payload = {
        **current,
        "name": candidate,
        "definition": deepcopy(current["definition"]),
        "default_settings": deepcopy(current["default_settings"]),
    }
    payload.pop("id", None)
    return create_strategy(payload)


def validate_saved_strategy(strategy_id: int) -> dict:
    strategy = backtest_repository.get_strategy(strategy_id)
    validated = _validate_code_configuration(validate_strategy_payload(strategy))
    return {
        "ok": True,
        "strategy": validated,
        "message": "策略结构、公式和参数校验通过；行情完整性将在运行前检查。",
    }


def _compact_number(value: object) -> str:
    number = float(value)
    return f"{number:g}"


def build_run_configuration_summary(strategy: dict, settings: dict) -> str:
    common = (
        f"{settings['start_date']}至{settings['end_date']}，初始资金"
        f"${settings['initial_capital']:,.2f}，{settings['leverage_multiplier']:g}倍杠杆，"
        f"每股手续费${settings['commission_per_share']:g}/最低${settings['minimum_commission']:g}，"
        f"滑点{settings['slippage_bps']:g}bps，"
        f"{'允许' if settings['allow_fractional_shares'] else '不允许'}碎股，"
        f"基准{settings['benchmark']}"
    )
    if strategy["design_mode"] == "code":
        strategy_type = get_code_strategy(strategy["code_key"])
        code_strategy = strategy_type(strategy["definition"].get("params", {}))
        detail = code_strategy.describe_run(strategy["definition"])
    else:
        symbols = "、".join(
            item["symbol"] for item in strategy["definition"]["symbols"]
        )
        rules = []
        for rule in strategy["definition"].get("rules", []):
            if not rule.get("enabled", True):
                continue
            if rule["action"] == "HOLD":
                result = "持有"
            else:
                result = (
                    f"{rule['action']} {rule['sizing_mode']} "
                    f"{_compact_number(rule['value'])}%"
                )
            rules.append(
                f"{rule['when']}若“{rule['condition']}”则{result}"
            )
        competition = strategy["definition"].get("competition")
        if competition:
            eligibility_timing = (
                f"{competition['eligibility_when']}检查候选“{competition['eligibility']}”，"
                if competition.get("eligibility_when", competition["when"])
                != competition["when"]
                else f"候选“{competition['eligibility']}”，"
            )
            minimum_score = (
                f"，最低评分{_compact_number(competition['minimum_score'])}（含）"
                if competition.get("minimum_score") is not None
                else ""
            )
            rules.append(
                f"{eligibility_timing}{competition['when']}按“{competition['score']}”竞争选标"
                f"{minimum_score}，目标{_compact_number(competition['target_weight'])}%"
            )
        detail = f"{strategy['selection_mode']}模式，标的{symbols}；{'；'.join(rules)}"
    return f"{common}；{detail}。"


class BacktestRunManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._states: dict[int, dict] = {}

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="backtest",
                )
            return self._executor

    def recover_interrupted_runs(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for run in backtest_repository.list_nonterminal_runs():
            backtest_repository.update_run(
                run["id"],
                status="failed",
                completed_at=now,
                error_code="PROCESS_INTERRUPTED",
                error_message="应用在回测完成前退出，本次运行已中止。",
            )

    def start(self, strategy_id: int, settings: dict | None = None) -> dict:
        strategy = backtest_repository.get_strategy(strategy_id)
        merged_settings = {
            **strategy["default_settings"],
            **(settings or {}),
        }
        validated_settings = validate_settings(merged_settings)
        run = backtest_repository.create_run(
            strategy,
            validated_settings,
            configuration_summary=build_run_configuration_summary(
                strategy, validated_settings
            ),
        )
        state = {
            "run_id": run["id"],
            "cancel": threading.Event(),
            "version": 0,
            "equity_points": [],
            "trades": [],
            "logs": [],
            "last_db_update": 0.0,
        }
        with self._lock:
            self._states[run["id"]] = state
        self._get_executor().submit(
            self._execute,
            run["id"],
            strategy,
            validated_settings,
        )
        return run

    def _execute(self, run_id: int, strategy: dict, settings: dict) -> None:
        engine: BacktestEngine | None = None
        state = self._states[run_id]
        try:
            if state["cancel"].is_set():
                raise BacktestCancelled("用户在任务开始前取消了回测。")
            backtest_repository.update_run(
                run_id,
                status="validating",
                started_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )

            def progress(payload: dict) -> None:
                now_monotonic = time.monotonic()
                with self._lock:
                    state["equity_points"] = list(engine.equity_points)
                    state["trades"] = list(engine.trades)
                    state["logs"] = list(engine.logs)
                    state["version"] += 1
                if now_monotonic - state["last_db_update"] >= 0.5:
                    backtest_repository.update_run(
                        run_id,
                        status="running",
                        progress=payload["progress"],
                        current_time=payload["current_time"],
                    )
                    state["last_db_update"] = now_monotonic

            engine = BacktestEngine(
                strategy,
                settings,
                progress_callback=progress,
                cancellation_check=state["cancel"].is_set,
            )
            backtest_repository.update_run(
                run_id,
                status="running",
                data_manifest=engine.dataset.manifest,
            )
            result = engine.run()
            backtest_repository.replace_run_output(
                run_id,
                equity_points=result.equity_points,
                trades=result.trades,
                logs=result.logs,
            )
            backtest_repository.update_run(
                run_id,
                status="completed",
                progress=1.0,
                current_time=(
                    result.liquidation.get("liquidation_time")
                    if result.liquidation
                    else engine.dataset.sessions[-1]
                ),
                data_manifest=result.data_manifest,
                metrics=result.metrics,
                termination_reason=result.termination_reason,
                completed_at=datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat(),
            )
            with self._lock:
                state["equity_points"] = list(result.equity_points)
                state["trades"] = list(result.trades)
                state["logs"] = list(result.logs)
                state["version"] += 1
        except BacktestCancelled as exc:
            if engine is not None:
                engine._log(
                    "INFO",
                    "RUN_CANCELLED",
                    exc.message,
                )
            self._persist_partial(
                run_id,
                engine,
                state,
                fallback_message=exc.message,
                fallback_code=exc.code,
            )
            backtest_repository.update_run(
                run_id,
                status="cancelled",
                error_code=exc.code,
                error_message=exc.message,
                completed_at=datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat(),
            )
            with self._lock:
                state["version"] += 1
        except Exception as exc:
            if isinstance(exc, BacktestError):
                code = exc.code
                message = exc.message
                detail = exc.detail
            else:
                code = "UNKNOWN_BACKTEST_ERROR"
                message = str(exc) or "回测运行发生未知错误。"
                detail = None
            if engine is not None:
                engine._log(
                    "ERROR",
                    "RUN_FAILED",
                    message,
                    context={"code": code, "detail": detail},
                )
            self._persist_partial(
                run_id,
                engine,
                state,
                fallback_message=message,
                fallback_code=code,
                fallback_detail=detail,
            )
            backtest_repository.update_run(
                run_id,
                status="failed",
                error_code=code,
                error_message=message,
                completed_at=datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat(),
            )
            with self._lock:
                state["version"] += 1

    def _persist_partial(
        self,
        run_id: int,
        engine: BacktestEngine | None,
        state: dict,
        *,
        fallback_message: str,
        fallback_code: str,
        fallback_detail: object | None = None,
    ) -> None:
        equity = list(engine.equity_points) if engine else []
        trades = list(engine.trades) if engine else []
        logs = list(engine.logs) if engine else []
        if not logs:
            logs = [
                {
                    "event_time": None,
                    "level": "ERROR",
                    "event_type": "RUN_FAILED",
                    "symbol": None,
                    "message": fallback_message,
                    "context": {
                        "code": fallback_code,
                        "detail": fallback_detail,
                    },
                }
            ]
        backtest_repository.replace_run_output(
            run_id,
            equity_points=equity,
            trades=trades,
            logs=logs,
        )
        with self._lock:
            state["equity_points"] = equity
            state["trades"] = trades
            state["logs"] = logs

    def cancel(self, run_id: int) -> dict:
        run = backtest_repository.get_run(run_id)
        if run["status"] in TERMINAL_STATUSES:
            return run
        with self._lock:
            state = self._states.get(run_id)
            if state:
                state["cancel"].set()
                state["version"] += 1
        return backtest_repository.request_run_cancellation(run_id)

    def run_status(self, run_id: int) -> dict:
        run = backtest_repository.get_run(run_id, include_snapshot=False)
        with self._lock:
            state = self._states.get(run_id)
            if state:
                run["live"] = {
                    "equity_point_count": len(state["equity_points"]),
                    "trade_count": len(state["trades"]),
                    "log_count": len(state["logs"]),
                    "latest_equity_point": (
                        state["equity_points"][-1]
                        if state["equity_points"]
                        else None
                    ),
                    "version": state["version"],
                }
        return run

    def result(self, run_id: int) -> dict:
        run = self.run_status(run_id)
        if run["status"] == "completed":
            return {
                "run": run,
                "equity_points": backtest_repository.get_equity_points(run_id),
                "trades": backtest_repository.get_trades(run_id),
            }
        with self._lock:
            state = self._states.get(run_id)
            if state is None:
                return {
                    "run": run,
                    "equity_points": backtest_repository.get_equity_points(run_id),
                    "trades": backtest_repository.get_trades(run_id),
                }
            return {
                "run": run,
                "equity_points": list(state["equity_points"]),
                "trades": list(state["trades"]),
            }

    def events_since(
        self,
        run_id: int,
        *,
        equity_after: int = 0,
        trade_after: int = 0,
        log_after: int = 0,
    ) -> dict:
        run = self.run_status(run_id)
        with self._lock:
            state = self._states.get(run_id)
            if state:
                equity = state["equity_points"][equity_after:]
                trades = state["trades"][trade_after:]
                logs = state["logs"][log_after:]
            else:
                equity = backtest_repository.get_equity_points(run_id)[equity_after:]
                trades = backtest_repository.get_trades(run_id)[trade_after:]
                logs = backtest_repository.get_logs(
                    run_id,
                    level="DEBUG",
                    after_sequence=log_after,
                    limit=5000,
                )
        return {
            "run": run,
            "equity_points": equity,
            "trades": trades,
            "logs": logs,
            "next": {
                "equity": equity_after + len(equity),
                "trade": trade_after + len(trades),
                "log": log_after + len(logs),
            },
        }

    def has_active_runs(self) -> bool:
        with self._lock:
            run_ids = list(self._states)
        for run_id in run_ids:
            try:
                if self.run_status(run_id)["status"] not in TERMINAL_STATUSES:
                    return True
            except ValueError:
                continue
        return False

    def purge_deleted_runs(self, run_ids: list[int]) -> None:
        with self._lock:
            for run_id in run_ids:
                self._states.pop(int(run_id), None)

    def shutdown(self) -> None:
        with self._lock:
            for state in self._states.values():
                state["cancel"].set()
            executor = self._executor
            self._executor = None
        if executor:
            # Wait until the worker has left its final repository call.  A
            # terminal status can become visible just before the thread exits;
            # returning earlier leaves SQLite handles briefly alive on Windows
            # and makes application/test shutdown nondeterministic.
            executor.shutdown(wait=True, cancel_futures=True)


run_manager = BacktestRunManager()


def code_strategy_catalog() -> list[dict]:
    return list_code_strategies()
