from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from database import realtime_repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.validation import validate_strategy_payload
from services.realtime_config import normalize_recommendation_state
from services.realtime_dashboard_service import build_database_decision_observation
from services.realtime_mail import normalize_recipients, render_message, send_smtp
from services.realtime_scheduler import (
    _events_for_strategy,
    run_manager,
    validate_realtime_code_version,
)


UTC = timezone.utc
TEST_SUBJECT_SUFFIX = "（测试）"
ACTIVE_RUNTIME_STATES = {"starting", "running", "degraded", "stopping"}
ACTIVE_RUN_STATES = {"starting", "running", "stopping"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_strategy_family(left: dict, right: dict) -> bool:
    return (
        left.get("design_mode") == right.get("design_mode")
        and left.get("code_key") == right.get("code_key")
    )


def _test_run(task: dict) -> tuple[dict, dict]:
    latest_run = task.get("latest_run")
    use_active_run = bool(
        latest_run
        and task.get("runtime_state") in ACTIVE_RUNTIME_STATES
        and latest_run.get("status") in ACTIVE_RUN_STATES
    )
    if use_active_run:
        strategy = deepcopy(latest_run["strategy_snapshot"])
        settings = deepcopy(latest_run.get("settings") or {})
        notification = deepcopy(latest_run.get("notification_settings") or {})
    else:
        strategy = deepcopy(task["strategy_snapshot"])
        settings = deepcopy(task.get("settings") or {})
        notification = deepcopy(task.get("notification_settings") or {})

    state = {"portfolio": deepcopy(task.get("portfolio_state") or {})}
    if latest_run and _same_strategy_family(
        latest_run.get("strategy_snapshot") or {}, strategy
    ):
        state = deepcopy(latest_run.get("state") or state)
        state["portfolio"] = normalize_recommendation_state(
            state.get("portfolio") or task.get("portfolio_state") or {}
        )

    run = {
        "id": int(latest_run["id"]) if latest_run else 0,
        "strategy_snapshot": strategy,
        "settings": settings,
        "notification_settings": notification,
        "state": state,
    }
    render_task = {
        **task,
        "strategy_snapshot": strategy,
        "settings": settings,
        "notification_settings": notification,
    }
    return run, render_task


def _test_subject(subject: str) -> str:
    return f"{subject[:200 - len(TEST_SUBJECT_SUFFIX)]}{TEST_SUBJECT_SUFFIX}"


def _observation_symbols(strategy: dict, run: dict) -> list[str]:
    symbols = [
        str(item["symbol"]).strip().upper()
        for item in strategy["definition"].get("symbols", [])
    ]
    portfolio = normalize_recommendation_state(
        (run.get("state") or {}).get("portfolio") or {}
    )
    symbols.extend(portfolio["recommended_targets"])
    if strategy["design_mode"] == "code":
        strategy_type = get_code_strategy(strategy["code_key"])
        params = strategy_type.validate_params(
            strategy["definition"].get("params", {})
        )
        symbols.extend(strategy_type.additional_symbols(params))
    return list(dict.fromkeys(symbols))


def send_current_decision_test_emails(
    task_id: int,
    *,
    channel_id: int,
    recipients,
    now: datetime | None = None,
) -> dict:
    """Evaluate and send every configured logical event from one current snapshot.

    The copied run state advances between events so multi-stage strategies such
    as WTME retain their normal risk-check/selection ordering. No realtime run,
    decision event, notification, cooldown or delayed-delivery row is changed.
    """
    observation_at = _as_utc(now or datetime.now(UTC)).replace(microsecond=0)
    task = run_manager.status(int(task_id))
    run, render_task = _test_run(task)
    strategy = validate_strategy_payload(deepcopy(run["strategy_snapshot"]))
    validate_realtime_code_version(strategy)
    events = _events_for_strategy(strategy)
    selected_events = {
        str(item).strip().upper()
        for item in (render_task["notification_settings"].get("events") or [])
        if str(item).strip()
    }
    if selected_events:
        events = [event for event in events if event.upper() in selected_events]
    if not events:
        raise ValueError("当前邮件设置没有选择任何可测试的策略事件。")

    recipient_list = normalize_recipients(recipients)
    realtime_repository.get_email_channel(int(channel_id))
    prepared_observation = build_database_decision_observation(
        strategy,
        _observation_symbols(strategy, run),
    )
    trading_date = str(prepared_observation["trading_date"])

    sent = []
    for event in events:
        result = run_manager.evaluator.evaluate(
            render_task,
            run,
            trading_date=trading_date,
            event=event,
            prepared_observation=prepared_observation,
        )
        # Advance only this in-memory copy. Formal task/run state remains intact.
        run["state"] = deepcopy(result["state"])
        subject, body = render_message(render_task, result)
        subject = _test_subject(subject)
        for recipient in recipient_list:
            provider_id = send_smtp(
                int(channel_id),
                recipient=recipient,
                subject=subject,
                body=body,
            )
            sent.append({
                "event": event,
                "recipient": recipient,
                "subject": subject,
                "provider_id": provider_id,
            })

    return {
        "task_id": int(task_id),
        "trading_date": trading_date,
        "observation_at": observation_at.isoformat().replace("+00:00", "Z"),
        "events": events,
        "recipient_count": len(recipient_list),
        "sent_count": len(sent),
        "messages": sent,
    }
