from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import threading
import time
from zoneinfo import ZoneInfo

from config import (
    REALTIME_EVENT_GRACE_SECONDS,
    REALTIME_MAX_WORKERS,
    REALTIME_RECOVERY_STALE_SECONDS,
)
from database import backtest_repository, realtime_repository, repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.market_calendar import ensure_market_sessions
from services.backtest.validation import validate_strategy_payload
from services.realtime_decision_service import RealtimeDecisionEvaluator
from services.realtime_mail import MailError, NotificationDispatcher
from services.realtime_market_data import IEXMarketDataHub
from services.realtime_panel_script import generate_panel_settings


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
TERMINAL_RUNTIME = {"stopped", "error"}


def _event_order(event: str) -> tuple[int, str]:
    if event == "OPEN":
        return (0, "09:30")
    if event == "CLOSE":
        return (2, "16:00")
    return (1, event)


def _events_for_strategy(strategy: dict) -> list[str]:
    definition = strategy["definition"]
    events: set[str] = set()
    if strategy["design_mode"] == "visual":
        for rule in definition.get("rules", []):
            if rule.get("enabled"):
                events.add(rule["when"])
        if strategy["selection_mode"] == "competition":
            events.add(definition["competition"]["when"])
    else:
        strategy_type = get_code_strategy(strategy["code_key"])
        events.update(strategy_type.required_events(definition.get("params", {})))
    return sorted(events, key=_event_order)


def _validate_local_history(strategy: dict) -> None:
    """Fail before launch when the local point-in-time history cannot support the formula."""
    definition = strategy["definition"]
    candidate_symbols = [str(item["symbol"]).upper() for item in definition.get("symbols", [])]
    symbols = list(candidate_symbols)
    auxiliary_symbols: list[str] = []
    minimum = 2
    if strategy["design_mode"] == "code":
        strategy_type = get_code_strategy(strategy["code_key"])
        params = strategy_type.validate_params(definition.get("params", {}))
        auxiliary_symbols = list(strategy_type.additional_symbols(params))
        symbols.extend(auxiliary_symbols)
        minimum = max(minimum, int(strategy_type.minimum_lookback(params)) + 1)
    available_candidates = 0
    for symbol in dict.fromkeys(symbols):
        rows = repository.get_daily_prices(symbol, include_metadata=True)
        if len(rows) < minimum:
            if strategy["selection_mode"] == "competition" and symbol in candidate_symbols and symbol not in auxiliary_symbols:
                continue
            raise ValueError(f"{symbol} 本地日线仅 {len(rows)} 根，策略至少需要 {minimum} 根；请先同步历史数据。")
        if symbol in candidate_symbols:
            available_candidates += 1
    if strategy["selection_mode"] == "competition" and available_candidates < 2:
        raise ValueError("competition 模式至少需要两个候选标的具备足够本地日线数据。")


def _session_at(sessions: list[dict], trading_date: str) -> dict | None:
    return next((item for item in sessions if item["trading_date"] == trading_date), None)


class RealtimeTaskManager:
    def __init__(self, *, max_workers: int = 4):
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="realtime-task")
        self._states: dict[int, dict] = {}
        self._session_cache: dict[str, list[dict]] = {}
        self.hub = IEXMarketDataHub()
        self.mail = NotificationDispatcher()
        self.evaluator = RealtimeDecisionEvaluator(self.hub)

    def start_services(self) -> None:
        self.hub.start()
        self.mail.start()
        self.recover_desired_tasks()

    def shutdown(self) -> None:
        with self._lock:
            states = list(self._states.values())
            for state in states:
                state["stop"].set()
        # A normal application shutdown must not leave a desired-running flag
        # behind. Otherwise the next process would interpret it as an
        # intentional recovery and start the task again.
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._lock:
            active_ids = set(self._states)
        for task in realtime_repository.list_tasks():
            if task["desired_state"] == "running":
                try:
                    realtime_repository.set_task_runtime(
                        task["id"], desired_state="stopped",
                        runtime_state="stopping" if task["id"] in active_ids else "stopped",
                        stopped_at=None if task["id"] in active_ids else now,
                        heartbeat_at=now,
                    )
                except ValueError:
                    pass
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.mail.stop()
        self.hub.stop()

    def recover_desired_tasks(self) -> None:
        for task in realtime_repository.list_tasks():
            if task["desired_state"] == "running":
                if self._is_stale_runtime(task):
                    now = datetime.now(UTC).replace(microsecond=0).isoformat()
                    realtime_repository.set_task_runtime(
                        task["id"], desired_state="stopped", runtime_state="stopped",
                        stopped_at=now, error_code="STALE_AFTER_RESTART",
                        error_message="检测到上次进程已停止但任务未正常收尾，已安全置为停止。",
                    )
                    continue
                try:
                    self.start(task["id"], recovering=True)
                except Exception:
                    realtime_repository.set_task_runtime(
                        task["id"], runtime_state="error", error_code="RECOVERY_FAILED", error_message="应用重启后任务恢复失败。"
                    )

    @staticmethod
    def _is_stale_runtime(task: dict) -> bool:
        heartbeat = task.get("heartbeat_at") or task.get("run_started_at")
        if not heartbeat:
            return True
        try:
            value = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
        except ValueError:
            return True
        return (datetime.now(UTC) - value).total_seconds() > REALTIME_RECOVERY_STALE_SECONDS

    def start(self, task_id: int, *, recovering: bool = False) -> dict:
        task = self._sync_followed_strategy(realtime_repository.get_task(task_id))
        strategy = validate_strategy_payload(task["strategy_snapshot"])
        _validate_local_history(strategy)
        events = _events_for_strategy(strategy)
        if not events:
            raise ValueError("策略没有可执行的实时事件。")
        if task["runtime_state"] in {"starting", "running", "degraded", "stopping"}:
            return task
        run = realtime_repository.create_run(task)
        stop = threading.Event()
        with self._lock:
            self._states[int(task_id)] = {"stop": stop, "run_id": run["id"], "last_event_key": None}
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        realtime_repository.set_task_runtime(
            task_id, desired_state="running", runtime_state="starting", run_started_at=now,
            stopped_at=None, heartbeat_at=now, error_code=None, error_message=None,
        )
        realtime_repository.update_run(run["id"], status="starting", heartbeat_at=now)
        self._executor.submit(self._execute, int(task_id), int(run["id"]), events)
        return realtime_repository.get_task(task_id)

    def stop(self, task_id: int) -> dict:
        task = realtime_repository.get_task(task_id)
        with self._lock:
            state = self._states.get(int(task_id))
            if state:
                state["stop"].set()
        stopped_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        realtime_repository.set_task_runtime(
            task_id,
            desired_state="stopped",
            runtime_state="stopping" if state else "stopped",
            stopped_at=None if state else stopped_at,
        )
        return realtime_repository.get_task(task_id)

    def status(self, task_id: int) -> dict:
        task = realtime_repository.get_task(task_id)
        runs = realtime_repository.list_runs(task_id, limit=1)
        if runs:
            task["latest_run"] = runs[0]
        return task

    def _sync_followed_strategy(self, task: dict) -> dict:
        if not task["follow_strategy"]:
            return task
        strategy = backtest_repository.get_strategy(task["strategy_id"])
        if (
            int(task["source_strategy_revision"]) == int(strategy["revision"])
            and task["strategy_snapshot"].get("code_version") == strategy.get("code_version")
        ):
            return task
        updated = realtime_repository.update_task(
            task["id"], strategy_snapshot=strategy,
            source_strategy_revision=strategy["revision"], source_code_version=strategy.get("code_version"),
        )
        if (
            strategy.get("design_mode") == "visual"
            and not (task.get("panel_settings") or {}).get("customized")
        ):
            updated = realtime_repository.update_panel_settings(
                task["id"], generate_panel_settings(strategy)
            )
        return updated

    def _sessions_for(self, start: date, end: date) -> list[dict]:
        key = f"{start.isoformat()}:{end.isoformat()}"
        if key not in self._session_cache:
            self._session_cache[key] = ensure_market_sessions(start.isoformat(), end.isoformat())
        return self._session_cache[key]

    def _next_event(self, strategy: dict, now: datetime) -> tuple[dict, str] | None:
        local_date = now.astimezone(NEW_YORK).date()
        sessions = self._sessions_for(local_date, local_date + timedelta(days=10))
        for session in sessions:
            trading_date = session["trading_date"]
            for event in _events_for_strategy(strategy):
                if event == "OPEN":
                    target = datetime.fromtimestamp(int(session["open_minute_utc"]) * 60, tz=UTC)
                elif event == "CLOSE":
                    target = datetime.fromtimestamp(int(session["close_minute_utc"]) * 60, tz=UTC)
                else:
                    parsed = datetime.combine(date.fromisoformat(trading_date), datetime.strptime(event, "%H:%M").time(), tzinfo=NEW_YORK)
                    target = parsed.astimezone(UTC)
                    if target < datetime.fromtimestamp(int(session["open_minute_utc"]) * 60, tz=UTC) or target >= datetime.fromtimestamp(int(session["close_minute_utc"]) * 60, tz=UTC):
                        if strategy["design_mode"] == "code":
                            strategy_type = get_code_strategy(strategy["code_key"])
                            offset = strategy_type.early_close_offsets(strategy["definition"].get("params", {})).get(event)
                            if offset is not None and session.get("is_early_close"):
                                target = datetime.fromtimestamp((int(session["close_minute_utc"]) - int(offset)) * 60, tz=UTC)
                            else:
                                continue
                        else:
                            continue
                if target >= now - timedelta(seconds=60):
                    return {"trading_date": trading_date, "target": target, "session": session}, event
        return None

    def _execute(self, task_id: int, run_id: int, events: list[str]) -> None:
        state = self._states[task_id]
        try:
            task = realtime_repository.get_task(task_id)
            strategy = task["strategy_snapshot"]
            realtime_repository.update_run(run_id, status="running", heartbeat_at=datetime.now(UTC).replace(microsecond=0).isoformat())
            realtime_repository.set_task_runtime(task_id, runtime_state="running", heartbeat_at=datetime.now(UTC).replace(microsecond=0).isoformat())
            processed: set[str] = set()
            while not state["stop"].is_set():
                task = realtime_repository.get_task(task_id)
                if task["desired_state"] != "running":
                    break
                next_item = self._next_event(strategy, datetime.now(UTC))
                if next_item is None:
                    state["stop"].wait(30)
                    continue
                info, event = next_item
                key = f"{info['trading_date']}|{event}|{info['target'].isoformat()}"
                realtime_repository.set_task_runtime(task_id, next_event_at=info["target"].isoformat().replace("+00:00", "Z"), heartbeat_at=datetime.now(UTC).replace(microsecond=0).isoformat())
                wait = (info["target"] - datetime.now(UTC)).total_seconds()
                if wait > 0:
                    state["stop"].wait(min(wait, 5.0))
                    continue
                if key in processed:
                    state["stop"].wait(1)
                    continue
                processed.add(key)
                lateness = max(0.0, (datetime.now(UTC) - info["target"]).total_seconds())
                self._run_event(task, run_id, info["trading_date"], event, info["target"], lateness)
                state["last_event_key"] = key
        except Exception as exc:
            realtime_repository.update_run(run_id, status="failed", error_code="TASK_FAILED", error_message=str(exc), stopped_at=datetime.now(UTC).replace(microsecond=0).isoformat())
            realtime_repository.set_task_runtime(task_id, runtime_state="error", error_code="TASK_FAILED", error_message=str(exc), stopped_at=datetime.now(UTC).replace(microsecond=0).isoformat())
        finally:
            with self._lock:
                self._states.pop(task_id, None)
            task = realtime_repository.get_task(task_id)
            if task["desired_state"] == "stopped" and task["runtime_state"] != "error":
                now = datetime.now(UTC).replace(microsecond=0).isoformat()
                realtime_repository.set_task_runtime(task_id, runtime_state="stopped", stopped_at=now)
                try:
                    realtime_repository.update_run(run_id, status="stopped", stopped_at=now, heartbeat_at=now)
                except ValueError:
                    pass

    def _run_event(self, task: dict, run_id: int, trading_date: str, event: str, scheduled_at: datetime, lateness: float) -> None:
        dedupe = f"task:{task['id']}:run:{run_id}:{trading_date}:{event}:{scheduled_at.isoformat()}"
        decision_event = realtime_repository.create_event(
            run_id=run_id, task_id=task["id"], dedupe_key=dedupe,
            trading_date=trading_date, event_name=event,
            scheduled_at=scheduled_at.isoformat().replace("+00:00", "Z"),
        )
        if decision_event["status"] == "completed":
            return
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        realtime_repository.update_event(decision_event["id"], status="running", started_at=now)
        if lateness > REALTIME_EVENT_GRACE_SECONDS:
            realtime_repository.update_event(decision_event["id"], status="skipped", completed_at=now, error_code="MISSED_EVENT", error_message="应用或行情恢复超过允许窗口，未使用过时数据补算。")
            return
        try:
            run = realtime_repository.get_run(run_id)
            result = self.evaluator.evaluate(task, run, trading_date=trading_date, event=event)
            # A run is immutable: edits made to the task while it is active
            # take effect only after stop/start, including notification rules.
            run_task = {
                **task,
                "strategy_snapshot": run["strategy_snapshot"],
                "settings": run["settings"],
                "notification_settings": run["notification_settings"],
            }
            subject, body = (None, None)
            if run_task["notification_settings"].get("enabled"):
                from services.realtime_mail import render_message
                subject, body = render_message(run_task, result)
            realtime_repository.update_event(
                decision_event["id"], status="completed", completed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                data_manifest=result["data_manifest"], decision=result["decision"],
                calculation=result["calculation"], message_subject=subject, message_body=body,
            )
            realtime_repository.update_run(
                run_id, status="running", state=result["state"], heartbeat_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                last_event_at=trading_date + " " + event,
            )
            realtime_repository.update_portfolio_state(
                task["id"], result["state"].get("portfolio") or task["portfolio_state"],
            )
            event = realtime_repository.get_event(decision_event["id"])
            if run_task["notification_settings"].get("enabled"):
                self.mail.enqueue_for_event(run_task, event, result)
        except Exception as exc:
            realtime_repository.update_event(
                decision_event["id"], status="failed", completed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                error_code=getattr(exc, "code", "DECISION_FAILED"), error_message=str(exc),
            )
            realtime_repository.set_task_runtime(task["id"], runtime_state="degraded", error_code="DECISION_FAILED", error_message=str(exc))

    def has_active_tasks(self) -> bool:
        return realtime_repository.active_task_count() > 0


run_manager = RealtimeTaskManager(max_workers=REALTIME_MAX_WORKERS)
