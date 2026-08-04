from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any

from database.db import get_connection
from database.repository import utc_now_iso


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _task_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for column, key, default in (
        ("strategy_snapshot_json", "strategy_snapshot", {}),
        ("settings_json", "settings", {}),
        ("notification_settings_json", "notification_settings", {}),
        ("portfolio_state_json", "portfolio_state", {}),
    ):
        item[key] = _decode(item.pop(column), default)
    return item


def _run_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for column, key, default in (
        ("strategy_snapshot_json", "strategy_snapshot", {}),
        ("settings_json", "settings", {}),
        ("notification_settings_json", "notification_settings", {}),
        ("state_json", "state", {}),
    ):
        item[key] = _decode(item.pop(column), default)
    return item


def _event_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    for column, key, default in (
        ("data_manifest_json", "data_manifest", None),
        ("decision_json", "decision", None),
        ("calculation_json", "calculation", None),
    ):
        item[key] = _decode(item.pop(column), default)
    return item


def list_tasks(*, include_deleted: bool = False) -> list[dict]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM realtime_decision_tasks
            {where}
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    return [_task_row(row) for row in rows]


def get_task(task_id: int, *, include_deleted: bool = False) -> dict:
    deleted = "" if include_deleted else "AND deleted_at IS NULL"
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT * FROM realtime_decision_tasks WHERE id = ? {deleted}",
            (int(task_id),),
        ).fetchone()
    if not row:
        raise ValueError("实时决策任务不存在或已删除。")
    return _task_row(row)


def create_task(
    *,
    name: str,
    strategy: dict,
    follow_strategy: bool,
    settings: dict,
    notification_settings: dict,
    portfolio_state: dict,
) -> dict:
    now = utc_now_iso()
    snapshot = {key: value for key, value in strategy.items() if key != "deleted_at"}
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO realtime_decision_tasks (
                    name, strategy_id, follow_strategy,
                    source_strategy_revision, source_code_version,
                    strategy_snapshot_json, settings_json,
                    notification_settings_json, portfolio_state_json,
                    desired_state, runtime_state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped', 'stopped', ?, ?)
                """,
                (
                    str(name).strip(),
                    int(strategy["id"]),
                    int(bool(follow_strategy)),
                    int(strategy["revision"]),
                    strategy.get("code_version"),
                    _json(snapshot),
                    _json(settings),
                    _json(notification_settings),
                    _json(portfolio_state),
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        if "unique" in str(exc).lower() or "name" in str(exc).lower():
            raise ValueError("实时决策任务名称已存在。") from exc
        raise
    return get_task(task_id)


def update_task(
    task_id: int,
    *,
    name: str | None = None,
    strategy_snapshot: dict | None = None,
    source_strategy_revision: int | None = None,
    source_code_version: str | None = None,
    follow_strategy: bool | None = None,
    settings: dict | None = None,
    notification_settings: dict | None = None,
    portfolio_state: dict | None = None,
    expected_revision: int | None = None,
) -> dict:
    current = get_task(task_id)
    if expected_revision is not None and int(current["revision"]) != int(expected_revision):
        raise RuntimeError("实时决策任务已在其他位置被修改，请刷新后重试。")
    values = {
        "name": name if name is not None else current["name"],
        "strategy_snapshot": strategy_snapshot or current["strategy_snapshot"],
        "source_strategy_revision": (
            source_strategy_revision
            if source_strategy_revision is not None
            else current["source_strategy_revision"]
        ),
        "source_code_version": (
            source_code_version
            if source_code_version is not None
            else current["source_code_version"]
        ),
        "follow_strategy": (
            bool(follow_strategy)
            if follow_strategy is not None
            else bool(current["follow_strategy"])
        ),
        "settings": settings or current["settings"],
        "notification_settings": (
            notification_settings
            if notification_settings is not None
            else current["notification_settings"]
        ),
        "portfolio_state": portfolio_state or current["portfolio_state"],
    }
    now = utc_now_iso()
    try:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE realtime_decision_tasks
                SET name = ?, strategy_snapshot_json = ?,
                    source_strategy_revision = ?, source_code_version = ?,
                    follow_strategy = ?, settings_json = ?,
                    notification_settings_json = ?, portfolio_state_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    values["name"],
                    _json(values["strategy_snapshot"]),
                    int(values["source_strategy_revision"]),
                    values["source_code_version"],
                    int(values["follow_strategy"]),
                    _json(values["settings"]),
                    _json(values["notification_settings"]),
                    _json(values["portfolio_state"]),
                    now,
                    int(task_id),
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "unique" in str(exc).lower() or "name" in str(exc).lower():
            raise ValueError("实时决策任务名称已存在。") from exc
        raise
    return get_task(task_id)


def update_portfolio_state(task_id: int, portfolio_state: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE realtime_decision_tasks SET portfolio_state_json = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_json(portfolio_state), utc_now_iso(), int(task_id)),
        )


def soft_delete_task(task_id: int) -> dict:
    task = get_task(task_id)
    if task["runtime_state"] not in {"stopped", "error"}:
        raise ValueError("任务必须停止后才能删除。")
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            "UPDATE realtime_decision_tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, int(task_id)),
        )
    return {**task, "deleted_at": now}


def set_task_runtime(
    task_id: int,
    *,
    desired_state: str | None = None,
    runtime_state: str | None = None,
    run_started_at: str | None = None,
    stopped_at: str | None = None,
    heartbeat_at: str | None = None,
    next_event_at: str | None = None,
    last_event_at: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    successful_notification_count: int | None = None,
    next_allowed_normal_send_at: str | None = None,
) -> dict:
    current = get_task(task_id)
    values = {
        "desired_state": desired_state or current["desired_state"],
        "runtime_state": runtime_state or current["runtime_state"],
        "run_started_at": run_started_at if run_started_at is not None else current["run_started_at"],
        "stopped_at": stopped_at if stopped_at is not None else current["stopped_at"],
        "heartbeat_at": heartbeat_at if heartbeat_at is not None else current["heartbeat_at"],
        "next_event_at": next_event_at if next_event_at is not None else current["next_event_at"],
        "last_event_at": last_event_at if last_event_at is not None else current["last_event_at"],
        "error_code": error_code if error_code is not None else current["last_error_code"],
        "error_message": error_message if error_message is not None else current["last_error_message"],
        "count": successful_notification_count if successful_notification_count is not None else current["successful_notification_count"],
        "next_allowed": next_allowed_normal_send_at if next_allowed_normal_send_at is not None else current["next_allowed_normal_send_at"],
    }
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE realtime_decision_tasks
            SET desired_state = ?, runtime_state = ?, run_started_at = ?, stopped_at = ?,
                heartbeat_at = ?, next_event_at = ?, last_event_at = ?,
                last_error_code = ?, last_error_message = ?,
                successful_notification_count = ?, next_allowed_normal_send_at = ?,
                updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (
                values["desired_state"], values["runtime_state"], values["run_started_at"],
                values["stopped_at"], values["heartbeat_at"], values["next_event_at"],
                values["last_event_at"], values["error_code"], values["error_message"],
                int(values["count"]), values["next_allowed"], utc_now_iso(), int(task_id),
            ),
        )
    return get_task(task_id)


def create_run(task: dict, *, started_at: str | None = None) -> dict:
    started = started_at or utc_now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO realtime_decision_runs (
                task_id, strategy_snapshot_json, settings_json,
                notification_settings_json, state_json, status,
                started_at, heartbeat_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?)
            """,
            (
                int(task["id"]), _json(task["strategy_snapshot"]),
                _json(task["settings"]), _json(task["notification_settings"]),
                _json({"portfolio": task.get("portfolio_state") or {}}),
                started, started, started,
            ),
        )
        run_id = int(cursor.lastrowid)
    return get_run(run_id)


def get_run(run_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM realtime_decision_runs WHERE id = ?", (int(run_id),)
        ).fetchone()
    if not row:
        raise ValueError("实时决策运行不存在。")
    return _run_row(row)


def list_runs(task_id: int, *, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM realtime_decision_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (int(task_id), int(limit)),
        ).fetchall()
    return [_run_row(row) for row in rows]


def update_run(
    run_id: int,
    *,
    status: str | None = None,
    state: dict | None = None,
    heartbeat_at: str | None = None,
    last_event_at: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    stopped_at: str | None = None,
) -> dict:
    current = get_run(run_id)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE realtime_decision_runs
            SET status = ?, state_json = ?, heartbeat_at = ?, last_event_at = ?,
                last_error_code = ?, last_error_message = ?, stopped_at = ?
            WHERE id = ?
            """,
            (
                status or current["status"], _json(state if state is not None else current["state"]),
                heartbeat_at if heartbeat_at is not None else current["heartbeat_at"],
                last_event_at if last_event_at is not None else current["last_event_at"],
                error_code if error_code is not None else current["last_error_code"],
                error_message if error_message is not None else current["last_error_message"],
                stopped_at if stopped_at is not None else current["stopped_at"], int(run_id),
            ),
        )
    return get_run(run_id)


def create_event(
    *, run_id: int, task_id: int, dedupe_key: str, trading_date: str,
    event_name: str, scheduled_at: str,
) -> dict:
    now = utc_now_iso()
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO realtime_decision_events (
                    run_id, task_id, dedupe_key, trading_date, event_name,
                    scheduled_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (int(run_id), int(task_id), dedupe_key, trading_date, event_name, scheduled_at, now),
            )
            event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM realtime_decision_events WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            return _event_row(row)
    return get_event(event_id)


def get_event(event_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM realtime_decision_events WHERE id = ?", (int(event_id),)
        ).fetchone()
    if not row:
        raise ValueError("实时决策事件不存在。")
    return _event_row(row)


def update_event(event_id: int, **fields) -> dict:
    allowed = {
        "status", "started_at", "completed_at", "data_manifest_json", "decision_json",
        "calculation_json", "message_subject", "message_body", "error_code", "error_message",
    }
    aliases = {
        "data_manifest": "data_manifest_json",
        "decision": "decision_json",
        "calculation": "calculation_json",
    }
    values = {
        aliases.get(key, key): value
        for key, value in fields.items()
        if aliases.get(key, key) in allowed
    }
    if not values:
        return get_event(event_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = [
        _json(value) if key.endswith("_json") and value is not None else value
        for key, value in values.items()
    ]
    params.append(int(event_id))
    with get_connection() as conn:
        conn.execute(f"UPDATE realtime_decision_events SET {assignments} WHERE id = ?", params)
    return get_event(event_id)


def list_events(task_id: int, *, limit: int = 100, before_id: int | None = None) -> list[dict]:
    clause = "AND id < ?" if before_id is not None else ""
    params = [int(task_id)] + ([int(before_id)] if before_id is not None else []) + [int(limit)]
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM realtime_decision_events
            WHERE task_id = ? {clause}
            ORDER BY id DESC LIMIT ?
            """, params
        ).fetchall()
    return [_event_row(row) for row in rows]


def create_email_channel(payload: dict, *, secret_ciphertext: str | None) -> dict:
    now = utc_now_iso()
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO email_channels (
                    name, provider, sender_email, smtp_host, smtp_port,
                    security_mode, username, secret_ciphertext, secret_key_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"], payload["provider"], payload["sender_email"],
                    payload["smtp_host"], int(payload["smtp_port"]), payload["security_mode"],
                    payload["username"], secret_ciphertext, payload.get("secret_key_id"), now, now,
                ),
            )
            channel_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("邮件通道名称已存在。") from exc
    return get_email_channel(channel_id)


def get_email_channel(channel_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM email_channels WHERE id = ?", (int(channel_id),)).fetchone()
    if not row:
        raise ValueError("邮件通道不存在。")
    item = dict(row)
    item["has_secret"] = bool(item.pop("secret_ciphertext"))
    item.pop("secret_key_id", None)
    return item


def get_email_channel_secret(channel_id: int) -> tuple[dict, str | None]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM email_channels WHERE id = ?", (int(channel_id),)).fetchone()
    if not row:
        raise ValueError("邮件通道不存在。")
    item = dict(row)
    secret = item.pop("secret_ciphertext")
    item.pop("secret_key_id", None)
    item["has_secret"] = bool(secret)
    return item, secret


def update_email_channel(channel_id: int, payload: dict, *, secret_ciphertext: str | None = None) -> dict:
    current, current_secret = get_email_channel_secret(channel_id)
    secret = secret_ciphertext if secret_ciphertext is not None else current_secret
    fields = {
        "name": payload.get("name", current["name"]),
        "provider": payload.get("provider", current["provider"]),
        "sender_email": payload.get("sender_email", current["sender_email"]),
        "smtp_host": payload.get("smtp_host", current["smtp_host"]),
        "smtp_port": int(payload.get("smtp_port", current["smtp_port"])),
        "security_mode": payload.get("security_mode", current["security_mode"]),
        "username": payload.get("username", current["username"]),
    }
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE email_channels
            SET name = ?, provider = ?, sender_email = ?, smtp_host = ?, smtp_port = ?,
                security_mode = ?, username = ?, secret_ciphertext = ?, updated_at = ?
            WHERE id = ?
            """,
            (*fields.values(), secret, utc_now_iso(), int(channel_id)),
        )
    return get_email_channel(channel_id)


def mark_email_channel_test(channel_id: int, *, ok: bool, error: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE email_channels SET last_test_at = ?, last_test_ok = ?, last_error = ? WHERE id = ?",
            (utc_now_iso(), int(ok), error, int(channel_id)),
        )


def reserve_normal_send(task_id: int, *, now: datetime, cooldown_seconds: int = 60) -> bool:
    """Atomically reserve a normal-send slot; retry deliveries do not call this."""
    now = now.astimezone(timezone.utc)
    now_text = now.replace(microsecond=0).isoformat()
    next_text = (now + timedelta(seconds=cooldown_seconds)).replace(microsecond=0).isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT next_allowed_normal_send_at FROM realtime_decision_tasks WHERE id = ? AND deleted_at IS NULL",
            (int(task_id),),
        ).fetchone()
        if not row:
            raise ValueError("实时决策任务不存在。")
        current = row["next_allowed_normal_send_at"]
        if current:
            try:
                if datetime.fromisoformat(current.replace("Z", "+00:00")) > now:
                    conn.rollback()
                    return False
            except ValueError:
                pass
        conn.execute(
            "UPDATE realtime_decision_tasks SET next_allowed_normal_send_at = ?, updated_at = ? WHERE id = ?",
            (next_text, now_text, int(task_id)),
        )
        conn.commit()
    return True


def create_notification(
    *, event_id: int, task_id: int, channel_id: int, recipient: str,
    subject: str, body: str, dedupe_key: str, is_retry: bool = False,
) -> dict:
    now = utc_now_iso()
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO realtime_notifications (
                    event_id, task_id, channel_id, recipient, subject, body,
                    dedupe_key, is_retry, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(event_id), int(task_id), int(channel_id), recipient, subject, body,
                 dedupe_key, int(is_retry), now, now),
            )
            notification_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM realtime_notifications WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            return dict(row)
    return get_notification(notification_id)


def get_notification(notification_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM realtime_notifications WHERE id = ?", (int(notification_id),)).fetchone()
    if not row:
        raise ValueError("邮件通知记录不存在。")
    return dict(row)


def list_notifications(task_id: int, *, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM realtime_notifications WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (int(task_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def update_notification(notification_id: int, **fields) -> dict:
    allowed = {
        "status", "is_retry", "attempt_count", "next_attempt_at", "sent_at",
        "provider_message_id", "error_code", "error_message",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return get_notification(notification_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = [*values.values(), utc_now_iso(), int(notification_id)]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE realtime_notifications SET {assignments}, updated_at = ? WHERE id = ?",
            params,
        )
    return get_notification(notification_id)


def list_pending_notifications(*, now: str | None = None, limit: int = 50) -> list[dict]:
    now_text = now or utc_now_iso()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM realtime_notifications
            WHERE status IN ('queued', 'retrying')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id LIMIT ?
            """,
            (now_text, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def create_notification_attempt(notification_id: int, attempt_number: int) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO realtime_notification_attempts (notification_id, attempt_number, started_at, status) VALUES (?, ?, ?, 'failed')",
            (int(notification_id), int(attempt_number), utc_now_iso()),
        )
        return int(cursor.lastrowid)


def finish_notification_attempt(attempt_id: int, *, status: str, provider_message_id: str | None = None, error_code: str | None = None, error_message: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE realtime_notification_attempts SET completed_at = ?, status = ?, provider_message_id = ?, error_code = ?, error_message = ? WHERE id = ?",
            (utc_now_iso(), status, provider_message_id, error_code, error_message, int(attempt_id)),
        )


def increment_successful_notifications(task_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE realtime_decision_tasks SET successful_notification_count = successful_notification_count + 1, updated_at = ? WHERE id = ?",
            (utc_now_iso(), int(task_id)),
        )


def active_task_count() -> int:
    with get_connection() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM realtime_decision_tasks WHERE deleted_at IS NULL AND runtime_state IN ('starting', 'running', 'degraded', 'stopping')"
        ).fetchone()[0])
