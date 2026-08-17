from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import threading
from zoneinfo import ZoneInfo

from config import (
    REALTIME_EVENT_GRACE_SECONDS,
    REALTIME_MAX_WORKERS,
    REALTIME_RECOVERY_STALE_SECONDS,
)
from database import backtest_repository, realtime_repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.market_calendar import ensure_market_sessions
from services.backtest.validation import validate_strategy_payload
from services.realtime_decision_service import RealtimeDecisionEvaluator
from services.realtime_mail import NotificationDispatcher
from services.realtime_market_data import IEXMarketDataHub
from services.realtime_panel_script import generate_panel_settings
from services.market_data_request_coordinator import PRIORITY_FORMAL_DECISION
from services.market_data_service import refresh_symbol_daily_history
from services.realtime_history_service import prepare_strategy_history


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
SCHEDULER_FALLBACK_SECONDS = 30.0


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
            competition = definition["competition"]
            events.add(competition["when"])
            events.add(competition.get("eligibility_when", competition["when"]))
    else:
        strategy_type = get_code_strategy(strategy["code_key"])
        events.update(strategy_type.required_events(definition.get("params", {})))
    return sorted(events, key=_event_order)


def _validate_local_history(strategy: dict) -> None:
    """Repair and validate the exact completed-session window before launch."""
    current = datetime.now(UTC).astimezone(NEW_YORK)
    cutoff = current.date()
    if current.time() >= time(16, 20):
        cutoff += timedelta(days=1)
    prepare_strategy_history(
        strategy,
        trading_date=cutoff.isoformat(),
        refresh=lambda symbol, start_date: refresh_symbol_daily_history(
            symbol,
            start_date=start_date,
            priority=PRIORITY_FORMAL_DECISION,
        ),
    )


def _session_at(sessions: list[dict], trading_date: str) -> dict | None:
    return next((item for item in sessions if item["trading_date"] == trading_date), None)


class RealtimeTaskManager:
    def __init__(self, *, max_workers: int = 4):
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="realtime-task")
        self._states: dict[int, dict] = {}
        self._session_cache: dict[str, list[dict]] = {}
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self.hub = IEXMarketDataHub()
        self.mail = NotificationDispatcher()
        self.evaluator = RealtimeDecisionEvaluator(self.hub)

    def start_services(self) -> None:
        self.hub.start()
        self.mail.start()
        self._shutdown.clear()
        with self._lock:
            if not self._scheduler_thread or not self._scheduler_thread.is_alive():
                self._scheduler_thread = threading.Thread(
                    target=self._scheduler_loop,
                    name="realtime-scheduler",
                    daemon=True,
                )
                self._scheduler_thread.start()
        self.recover_desired_tasks()
        self._wake.set()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._wake.set()
        # A normal application shutdown must not leave a desired-running flag
        # behind. Otherwise the next process would interpret it as an
        # intentional recovery and start the task again.
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._lock:
            states = list(self._states.items())
            for _task_id, state in states:
                state["stop_requested"] = True
        state_by_task = dict(states)
        for task in realtime_repository.list_tasks():
            if task["desired_state"] == "running":
                try:
                    state = state_by_task.get(task["id"])
                    in_flight = bool(state and state.get("event_started"))
                    realtime_repository.set_task_runtime(
                        task["id"], desired_state="stopped",
                        runtime_state="stopping" if in_flight else "stopped",
                        stopped_at=None if in_flight else now,
                        heartbeat_at=now,
                        clear_next_event=True,
                    )
                    if state and not in_flight:
                        realtime_repository.update_run(
                            state["run_id"], status="stopped", stopped_at=now,
                            heartbeat_at=now,
                        )
                except ValueError:
                    pass
        thread = self._scheduler_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._scheduler_thread = None
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._states.clear()
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
        with self._lock:
            if int(task_id) in self._states:
                return task
        if (
            not recovering
            and task["runtime_state"] in {"starting", "running", "degraded", "stopping"}
        ):
            return task
        run = realtime_repository.create_run(task)
        state = {
            "run_id": int(run["id"]),
            "strategy": strategy,
            "events": events,
            "processed": set(),
            "event_in_flight": False,
            "event_started": False,
            "event_key": None,
            "future": None,
            "stop_requested": False,
        }
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        realtime_repository.set_task_runtime(
            task_id, desired_state="running", runtime_state="running",
            run_started_at=now, clear_stopped_at=True, heartbeat_at=now,
            clear_error=True,
        )
        realtime_repository.update_run(run["id"], status="running", heartbeat_at=now)
        with self._lock:
            self._states[int(task_id)] = state
        self._wake.set()
        return realtime_repository.get_task(task_id)

    def stop(self, task_id: int) -> dict:
        task = realtime_repository.get_task(task_id)
        state = None
        graceful = False
        with self._lock:
            state = self._states.get(int(task_id))
            if state:
                state["stop_requested"] = True
                graceful = bool(state.get("event_started"))
                if not graceful:
                    future = state.get("future")
                    if future is not None:
                        future.cancel()
                    self._states.pop(int(task_id), None)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        if graceful:
            realtime_repository.set_task_runtime(
                task_id, desired_state="stopped", runtime_state="stopping",
                clear_stopped_at=True, heartbeat_at=now, clear_next_event=True,
            )
            try:
                realtime_repository.update_run(
                    state["run_id"], status="stopping", heartbeat_at=now,
                )
            except ValueError:
                pass
        else:
            realtime_repository.set_task_runtime(
                task_id, desired_state="stopped", runtime_state="stopped",
                stopped_at=now, heartbeat_at=now, clear_next_event=True,
            )
            run_id = state.get("run_id") if state else None
            if run_id is None:
                runs = realtime_repository.list_runs(task["id"], limit=1)
                run_id = runs[0]["id"] if runs else None
            if run_id is not None:
                try:
                    realtime_repository.update_run(
                        run_id, status="stopped", stopped_at=now,
                        heartbeat_at=now,
                    )
                except ValueError:
                    pass
        self._wake.set()
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

    def _scheduler_loop(self) -> None:
        """Maintain all idle tasks with one wakeable scheduler thread."""
        while not self._shutdown.is_set():
            self._wake.clear()
            wait_seconds = self._schedule_once()
            self._wake.wait(timeout=max(0.05, wait_seconds))

    def _schedule_once(self, *, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        wait_seconds = SCHEDULER_FALLBACK_SECONDS
        with self._lock:
            scheduled = list(self._states.items())
        for task_id, state in scheduled:
            with self._lock:
                if self._states.get(task_id) is not state:
                    continue
                if state["stop_requested"] or state["event_in_flight"]:
                    continue
            try:
                task = realtime_repository.get_task(task_id)
                if task["desired_state"] != "running":
                    self._finish_stopped(task_id, state)
                    continue
                next_item = self._next_event(state["strategy"], current)
                if next_item is None:
                    continue
                info, event = next_item
                key = f"{info['trading_date']}|{event}|{info['target'].isoformat()}"
                heartbeat = datetime.now(UTC).replace(microsecond=0).isoformat()
                with self._lock:
                    if self._states.get(task_id) is not state or state["stop_requested"]:
                        continue
                    realtime_repository.set_task_runtime(
                        task_id,
                        next_event_at=info["target"].isoformat().replace("+00:00", "Z"),
                        heartbeat_at=heartbeat,
                    )
                    delay = (info["target"] - current).total_seconds()
                    if delay > 0:
                        wait_seconds = min(wait_seconds, delay)
                        continue
                    if key in state["processed"]:
                        wait_seconds = min(wait_seconds, 1.0)
                        continue
                    state["processed"].add(key)
                    state["event_in_flight"] = True
                    state["event_started"] = False
                    state["event_key"] = key
                    future = self._executor.submit(
                        self._execute_event, task_id, state, task, info, event,
                    )
                    if state["event_in_flight"] and state["event_key"] == key:
                        state["future"] = future
            except Exception as exc:
                self._fail_scheduled_task(task_id, state, exc)
        return wait_seconds

    def _execute_event(
        self, task_id: int, state: dict, task: dict, info: dict,
        event: str,
    ) -> None:
        with self._lock:
            if self._states.get(task_id) is not state:
                return
            if state["stop_requested"]:
                self._finish_stopped(task_id, state)
                return
            state["event_started"] = True
        unexpected_error: Exception | None = None
        try:
            lateness = max(
                0.0, (datetime.now(UTC) - info["target"]).total_seconds()
            )
            self._run_event(
                task, state["run_id"], info["trading_date"], event,
                info["target"], lateness,
            )
        except Exception as exc:
            unexpected_error = exc
        finally:
            with self._lock:
                if self._states.get(task_id) is not state:
                    return
                state["event_in_flight"] = False
                state["event_started"] = False
                state["future"] = None
                should_stop = state["stop_requested"]
            if should_stop:
                self._finish_stopped(task_id, state)
            else:
                if unexpected_error is not None:
                    try:
                        realtime_repository.set_task_runtime(
                            task_id, runtime_state="degraded",
                            error_code="DECISION_FAILED",
                            error_message=str(unexpected_error),
                        )
                    except ValueError:
                        pass
                self._wake.set()

    def _finish_stopped(self, task_id: int, state: dict) -> None:
        with self._lock:
            if self._states.get(task_id) is not state:
                return
            self._states.pop(task_id, None)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            realtime_repository.set_task_runtime(
                task_id, desired_state="stopped", runtime_state="stopped",
                stopped_at=now, heartbeat_at=now, clear_next_event=True,
            )
            realtime_repository.update_run(
                state["run_id"], status="stopped", stopped_at=now,
                heartbeat_at=now,
            )
        except ValueError:
            pass
        self._wake.set()

    def _fail_scheduled_task(self, task_id: int, state: dict, exc: Exception) -> None:
        with self._lock:
            if self._states.get(task_id) is not state:
                return
            self._states.pop(task_id, None)
        stopped_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            realtime_repository.update_run(
                state["run_id"], status="failed", error_code="TASK_FAILED",
                error_message=str(exc), stopped_at=stopped_at,
            )
            realtime_repository.set_task_runtime(
                task_id, desired_state="stopped", runtime_state="error",
                error_code="TASK_FAILED", error_message=str(exc),
                stopped_at=stopped_at, clear_next_event=True,
            )
        except ValueError:
            pass

    def _run_event(
        self, task: dict, run_id: int, trading_date: str, event: str,
        scheduled_at: datetime, lateness: float,
    ) -> None:
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
