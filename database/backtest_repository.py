from __future__ import annotations

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
    if value is None or value == "":
        return default
    return json.loads(value)


def _strategy_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item["definition"] = _decode(item.pop("definition_json"), {})
    item["default_settings"] = _decode(item.pop("default_settings_json"), {})
    return item


def _run_row(row: sqlite3.Row | dict, *, include_snapshot: bool = True) -> dict:
    item = dict(row)
    item["settings"] = _decode(item.pop("settings_json"), {})
    item["metrics"] = _decode(item.pop("metrics_json"), None)
    item["data_manifest"] = _decode(item.pop("data_manifest_json"), None)
    snapshot = _decode(item.pop("strategy_snapshot_json"), {})
    if include_snapshot:
        item["strategy_snapshot"] = snapshot
    return item


def list_strategies(*, include_deleted: bool = False) -> list[dict]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM backtest_strategies
            {where}
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    return [_strategy_row(row) for row in rows]


def get_strategy(strategy_id: int, *, include_deleted: bool = False) -> dict:
    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM backtest_strategies
            WHERE id = ? {deleted_clause}
            """,
            (int(strategy_id),),
        ).fetchone()
    if not row:
        raise ValueError("策略不存在或已删除。")
    return _strategy_row(row)


def create_strategy(payload: dict) -> dict:
    now = utc_now_iso()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO backtest_strategies (
                    name, description, design_mode, selection_mode,
                    code_key, code_version, definition_json,
                    default_settings_json, schema_version, revision,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    payload["name"],
                    payload.get("description"),
                    payload["design_mode"],
                    payload["selection_mode"],
                    payload.get("code_key"),
                    payload.get("code_version"),
                    _json(payload["definition"]),
                    _json(payload["default_settings"]),
                    int(payload.get("schema_version", 1)),
                    now,
                    now,
                ),
            )
            strategy_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        if "name" in str(exc).lower() or "unique" in str(exc).lower():
            raise ValueError("策略名称已存在。") from exc
        raise
    return get_strategy(strategy_id)


def seed_strategy_once(seed_key: str, payload: dict) -> dict | None:
    """Insert a shipped example once, without resurrecting it after hard deletion."""
    now = utc_now_iso()
    with get_connection() as conn:
        seeded = conn.execute(
            "SELECT strategy_id FROM backtest_strategy_seed_state WHERE seed_key = ?",
            (seed_key,),
        ).fetchone()
        if seeded:
            strategy_id = seeded["strategy_id"]
            if strategy_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM backtest_strategies WHERE id = ?",
                (int(strategy_id),),
            ).fetchone()
            return _strategy_row(row) if row else None
        try:
            cursor = conn.execute(
                """
                INSERT INTO backtest_strategies (
                    name, description, design_mode, selection_mode,
                    code_key, code_version, definition_json,
                    default_settings_json, schema_version, revision,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    payload["name"],
                    payload.get("description"),
                    payload["design_mode"],
                    payload["selection_mode"],
                    payload.get("code_key"),
                    payload.get("code_version"),
                    _json(payload["definition"]),
                    _json(payload["default_settings"]),
                    int(payload.get("schema_version", 1)),
                    now,
                    now,
                ),
            )
            strategy_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "name" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            existing = conn.execute(
                "SELECT id FROM backtest_strategies WHERE name = ? COLLATE NOCASE",
                (payload["name"],),
            ).fetchone()
            if not existing:
                raise
            strategy_id = int(existing["id"])
        conn.execute(
            """
            INSERT INTO backtest_strategy_seed_state (seed_key, strategy_id, seeded_at)
            VALUES (?, ?, ?)
            """,
            (seed_key, strategy_id, now),
        )
    return get_strategy(strategy_id)


def upgrade_seeded_strategy_settings_once(
    seed_key: str,
    upgrade_key: str,
    settings_patch: dict,
) -> dict | None:
    """Apply an intentional shipped-default change once, preserving other settings."""
    now = utc_now_iso()
    marker = f"upgrade:{upgrade_key}:{seed_key}"
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM backtest_strategy_seed_state WHERE seed_key = ?",
            (marker,),
        ).fetchone():
            return None
        seeded = conn.execute(
            "SELECT strategy_id FROM backtest_strategy_seed_state WHERE seed_key = ?",
            (seed_key,),
        ).fetchone()
        strategy_id = int(seeded["strategy_id"]) if seeded and seeded["strategy_id"] else None
        if strategy_id is not None:
            row = conn.execute(
                """
                SELECT default_settings_json
                FROM backtest_strategies
                WHERE id = ?
                """,
                (strategy_id,),
            ).fetchone()
            if row:
                settings = _decode(row["default_settings_json"], {})
                upgraded = {**settings, **settings_patch}
                if upgraded != settings:
                    conn.execute(
                        """
                        UPDATE backtest_strategies
                        SET default_settings_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (_json(upgraded), now, strategy_id),
                    )
        conn.execute(
            """
            INSERT INTO backtest_strategy_seed_state (seed_key, strategy_id, seeded_at)
            VALUES (?, ?, ?)
            """,
            (marker, strategy_id, now),
        )
    return get_strategy(strategy_id) if strategy_id is not None else None


def upgrade_seeded_strategy_code_version_once(
    seed_key: str,
    upgrade_key: str,
    *,
    code_key: str,
    from_versions: tuple[str, ...],
    to_version: str,
    parameter_defaults: dict | None = None,
    removed_parameters: tuple[str, ...] = (),
) -> dict | None:
    """Upgrade seeded code metadata and apply explicit parameter migrations."""
    now = utc_now_iso()
    marker = f"upgrade:{upgrade_key}:{seed_key}"
    allowed_versions = set(from_versions)
    strategy_id: int | None = None
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM backtest_strategy_seed_state WHERE seed_key = ?",
            (marker,),
        ).fetchone():
            return None
        seeded = conn.execute(
            "SELECT strategy_id FROM backtest_strategy_seed_state WHERE seed_key = ?",
            (seed_key,),
        ).fetchone()
        seeded_id = int(seeded["strategy_id"]) if seeded and seeded["strategy_id"] else None
        row = (
            conn.execute(
                """
                SELECT id, design_mode, code_key, code_version, definition_json
                FROM backtest_strategies
                WHERE id = ?
                """,
                (seeded_id,),
            ).fetchone()
            if seeded_id is not None
            else None
        )
        if row:
            strategy_id = int(row["id"])
            if (
                row["design_mode"] == "code"
                and row["code_key"] == code_key
                and row["code_version"] in allowed_versions
            ):
                definition = _decode(row["definition_json"], {})
                if parameter_defaults:
                    current_params = definition.get("params")
                    if not isinstance(current_params, dict):
                        current_params = {}
                    definition["params"] = {
                        **parameter_defaults,
                        **current_params,
                    }
                if removed_parameters:
                    current_params = definition.get("params")
                    if not isinstance(current_params, dict):
                        current_params = {}
                    definition["params"] = {
                        name: value
                        for name, value in current_params.items()
                        if name not in set(removed_parameters)
                    }
                conn.execute(
                    """
                    UPDATE backtest_strategies
                    SET code_version = ?, definition_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (to_version, _json(definition), now, strategy_id),
                )
        conn.execute(
            """
            INSERT INTO backtest_strategy_seed_state (seed_key, strategy_id, seeded_at)
            VALUES (?, ?, ?)
            """,
            (marker, strategy_id, now),
        )
    return get_strategy(strategy_id) if strategy_id is not None else None


def update_strategy(
    strategy_id: int,
    payload: dict,
    *,
    expected_revision: int | None = None,
) -> dict:
    current = get_strategy(strategy_id)
    if expected_revision is not None and current["revision"] != int(expected_revision):
        raise RuntimeError("策略已在其他位置被修改，请刷新后重试。")
    immutable = {"design_mode", "selection_mode"}
    for field in immutable:
        if field in payload and payload[field] != current[field]:
            raise ValueError("策略创建后不能修改设计模式或选标模式，请复制为新策略。")

    values = {
        "name": payload.get("name", current["name"]),
        "description": payload.get("description", current["description"]),
        "code_key": payload.get("code_key", current["code_key"]),
        "code_version": payload.get("code_version", current["code_version"]),
        "definition": payload.get("definition", current["definition"]),
        "default_settings": payload.get(
            "default_settings",
            current["default_settings"],
        ),
        "schema_version": int(payload.get("schema_version", current["schema_version"])),
    }
    try:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE backtest_strategies
                SET name = ?, description = ?, code_key = ?, code_version = ?,
                    definition_json = ?, default_settings_json = ?,
                    schema_version = ?, revision = revision + 1, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    values["name"],
                    values["description"],
                    values["code_key"],
                    values["code_version"],
                    _json(values["definition"]),
                    _json(values["default_settings"]),
                    values["schema_version"],
                    utc_now_iso(),
                    int(strategy_id),
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "name" in str(exc).lower() or "unique" in str(exc).lower():
            raise ValueError("策略名称已存在。") from exc
        raise
    return get_strategy(strategy_id)


def delete_strategy(strategy_id: int) -> dict:
    current = get_strategy(strategy_id)
    if current["design_mode"] == "code":
        raise ValueError("内置代码模式策略禁止删除。")
    with get_connection() as conn:
        live_task = conn.execute(
            """
            SELECT id FROM realtime_decision_tasks
            WHERE strategy_id = ? AND deleted_at IS NULL
            LIMIT 1
            """,
            (int(strategy_id),),
        ).fetchone()
        if live_task:
            raise ValueError("该策略仍被实时决策任务引用，请先停止并删除任务。")
        conn.execute(
            "UPDATE backtest_runs SET strategy_id = NULL WHERE strategy_id = ?",
            (int(strategy_id),),
        )
        conn.execute(
            "DELETE FROM backtest_strategies WHERE id = ?",
            (int(strategy_id),),
        )
    return current


def create_run(
    strategy: dict,
    settings: dict,
    *,
    configuration_summary: str | None = None,
) -> dict:
    snapshot = {
        key: value
        for key, value in strategy.items()
        if key not in {"deleted_at"}
    }
    now = utc_now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO backtest_runs (
                strategy_id, strategy_name, strategy_revision,
                strategy_snapshot_json, settings_json, status, progress,
                configuration_summary, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                int(strategy["id"]),
                strategy["name"],
                int(strategy["revision"]),
                _json(snapshot),
                _json(settings),
                configuration_summary,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
    return get_run(run_id)


def get_run(
    run_id: int,
    *,
    include_snapshot: bool = True,
    include_deleted: bool = False,
) -> dict:
    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT * FROM backtest_runs WHERE id = ? {deleted_clause}",
            (int(run_id),),
        ).fetchone()
    if not row:
        raise ValueError("回测运行不存在。")
    return _run_row(row, include_snapshot=include_snapshot)


def list_runs(strategy_id: int | None = None, *, limit: int = 50) -> list[dict]:
    params: list[int] = []
    clauses = ["deleted_at IS NULL"]
    if strategy_id is not None:
        clauses.append("strategy_id = ?")
        params.append(int(strategy_id))
    where = f"WHERE {' AND '.join(clauses)}"
    params.append(max(1, min(int(limit), 200)))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM backtest_runs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_run_row(row, include_snapshot=False) for row in rows]


def list_runs_overview(
    *,
    page: int = 1,
    page_size: int = 25,
    strategy_id: int | None = None,
    status: str | None = None,
) -> dict:
    clean_page = max(1, int(page))
    clean_size = max(1, min(int(page_size), 100))
    clauses: list[str] = ["deleted_at IS NULL"]
    params: list[Any] = []
    if strategy_id is not None:
        clauses.append("strategy_id = ?")
        params.append(int(strategy_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM backtest_runs {where}", params
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT *
            FROM backtest_runs
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, clean_size, (clean_page - 1) * clean_size),
        ).fetchall()
    items = []
    for row in rows:
        item = _run_row(row, include_snapshot=True)
        snapshot = item.pop("strategy_snapshot", {})
        item["symbols"] = [
            value.get("symbol")
            for value in snapshot.get("definition", {}).get("symbols", [])
            if value.get("symbol")
        ]
        items.append(item)
    return {
        "items": items,
        "page": clean_page,
        "page_size": clean_size,
        "total_rows": total,
        "total_pages": max(1, (total + clean_size - 1) // clean_size),
    }


def get_run_detail(run_id: int) -> dict:
    run = get_run(run_id, include_snapshot=True)
    with get_connection() as conn:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM backtest_equity_points WHERE run_id = ?) AS equity_count,
                (SELECT COUNT(*) FROM backtest_trades WHERE run_id = ?) AS trade_count,
                (SELECT COUNT(*) FROM backtest_logs WHERE run_id = ?) AS available_log_count
            """,
            (int(run_id), int(run_id), int(run_id)),
        ).fetchone()
    return {**run, **dict(counts)}


def delete_runs(run_ids: list[int]) -> dict:
    normalized = sorted({int(value) for value in run_ids if int(value) > 0})
    if not normalized:
        raise ValueError("请至少选择一条回测记录。")
    if len(normalized) > 200:
        raise ValueError("一次最多清理 200 条回测记录。")
    placeholders = ",".join("?" for _ in normalized)
    now = utc_now_iso()
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, status FROM backtest_runs
            WHERE id IN ({placeholders}) AND deleted_at IS NULL
            """,
            normalized,
        ).fetchall()
        found = {int(row["id"]): row["status"] for row in rows}
        missing = [run_id for run_id in normalized if run_id not in found]
        if missing:
            raise ValueError(f"回测运行不存在：{missing[0]}。")
        active = [
            run_id for run_id, run_status in found.items()
            if run_status not in {"completed", "failed", "cancelled"}
        ]
        if active:
            raise ValueError(f"运行中的回测不能删除：{active[0]}。")
        log_stats = conn.execute(
            f"""
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(LENGTH(message) + LENGTH(COALESCE(context_json, ''))), 0) AS bytes
            FROM backtest_logs
            WHERE run_id IN ({placeholders})
            """,
            normalized,
        ).fetchone()
        equity_stats = conn.execute(
            f"""
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(LENGTH(positions_json)), 0) AS bytes
            FROM backtest_equity_points
            WHERE run_id IN ({placeholders})
            """,
            normalized,
        ).fetchone()
        conn.execute(
            f"DELETE FROM backtest_logs WHERE run_id IN ({placeholders})",
            normalized,
        )
        conn.execute(
            f"DELETE FROM backtest_equity_points WHERE run_id IN ({placeholders})",
            normalized,
        )
        conn.execute(
            f"""
            UPDATE backtest_runs
            SET logs_deleted_at = COALESCE(logs_deleted_at, ?),
                deleted_at = ?
            WHERE id IN ({placeholders})
            """,
            (now, now, *normalized),
        )
    return {
        "run_ids": normalized,
        "deleted_log_rows": int(log_stats["rows"]),
        "deleted_log_bytes": int(log_stats["bytes"]),
        "deleted_equity_rows": int(equity_stats["rows"]),
        "deleted_equity_bytes": int(equity_stats["bytes"]),
        "deleted_at": now,
    }


def list_nonterminal_runs() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM backtest_runs
            WHERE status IN ('queued', 'validating', 'running', 'cancelling')
            ORDER BY id
            """
        ).fetchall()
    return [_run_row(row, include_snapshot=False) for row in rows]


def latest_runs_by_strategy() -> dict[int, dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT run.*
            FROM backtest_runs AS run
            INNER JOIN (
                SELECT strategy_id, MAX(id) AS latest_id
                FROM backtest_runs
                WHERE strategy_id IS NOT NULL
                  AND deleted_at IS NULL
                GROUP BY strategy_id
            ) AS latest
              ON latest.latest_id = run.id
            """
        ).fetchall()
    return {
        int(row["strategy_id"]): _run_row(row, include_snapshot=False)
        for row in rows
    }


def update_run(run_id: int, **fields: Any) -> dict:
    allowed = {
        "status",
        "progress",
        "current_time",
        "data_manifest",
        "metrics",
        "error_code",
        "error_message",
        "started_at",
        "completed_at",
        "termination_reason",
    }
    invalid = set(fields) - allowed
    if invalid:
        raise ValueError(f"Unsupported run fields: {sorted(invalid)}")
    if not fields:
        return get_run(run_id)
    encoded = dict(fields)
    for key in ("data_manifest", "metrics"):
        if key in encoded:
            value = encoded.pop(key)
            encoded[f"{key}_json"] = (
                _json(value) if value is not None else None
            )
    assignments = ", ".join(f"{name} = ?" for name in encoded)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE backtest_runs SET {assignments} WHERE id = ?",
            [*encoded.values(), int(run_id)],
        )
    return get_run(run_id)


def replace_run_output(
    run_id: int,
    *,
    equity_points: list[dict],
    trades: list[dict],
    logs: list[dict],
) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM backtest_equity_points WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM backtest_trades WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM backtest_logs WHERE run_id = ?", (run_id,))
        conn.executemany(
            """
            INSERT INTO backtest_equity_points (
                run_id, sequence, trading_date, cash, receivables,
                positions_value, equity, borrowed_cash, gross_leverage,
                return_rate, drawdown_rate, benchmark_equity,
                benchmark_return_rate, positions_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    index + 1,
                    point["trading_date"],
                    point["cash"],
                    point.get("receivables", 0),
                    point["positions_value"],
                    point["equity"],
                    point.get("borrowed_cash", max(0, -float(point["cash"]))),
                    point.get("gross_leverage", 0),
                    point["return_rate"],
                    point["drawdown_rate"],
                    point.get("benchmark_equity"),
                    point.get("benchmark_return_rate"),
                    _json(point.get("positions", {})),
                )
                for index, point in enumerate(equity_points)
            ],
        )
        conn.executemany(
            """
            INSERT INTO backtest_trades (
                run_id, sequence, event_time, symbol, side, quantity,
                reference_price, fill_price, gross_amount, commission,
                slippage_amount, realized_pnl, cash_after,
                position_quantity_after, position_value_after,
                position_weight_after, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    index + 1,
                    trade["event_time"],
                    trade["symbol"],
                    trade["side"],
                    trade["quantity"],
                    trade["reference_price"],
                    trade["fill_price"],
                    trade["gross_amount"],
                    trade["commission"],
                    trade["slippage_amount"],
                    trade.get("realized_pnl"),
                    trade["cash_after"],
                    trade["position_quantity_after"],
                    trade["position_value_after"],
                    trade["position_weight_after"],
                    trade.get("reason"),
                )
                for index, trade in enumerate(trades)
            ],
        )
        conn.executemany(
            """
            INSERT INTO backtest_logs (
                run_id, sequence, event_time, level, event_type, symbol,
                message, context_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    index + 1,
                    log.get("event_time"),
                    log["level"],
                    log["event_type"],
                    log.get("symbol"),
                    log["message"],
                    _json(log.get("context")) if log.get("context") is not None else None,
                    log.get("created_at") or utc_now_iso(),
                )
                for index, log in enumerate(logs)
            ],
        )
        log_bytes = sum(
            len(str(log.get("message") or ""))
            + len(_json(log.get("context")))
            if log.get("context") is not None
            else len(str(log.get("message") or ""))
            for log in logs
        )
        conn.execute(
            """
            UPDATE backtest_runs
            SET log_count = ?, log_bytes = ?, logs_deleted_at = NULL
            WHERE id = ?
            """,
            (len(logs), log_bytes, int(run_id)),
        )


def get_equity_points(run_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM backtest_equity_points
            WHERE run_id = ?
            ORDER BY sequence
            """,
            (int(run_id),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["positions"] = _decode(item.pop("positions_json"), {})
        result.append(item)
    return result


def get_trades(run_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM backtest_trades
            WHERE run_id = ?
            ORDER BY sequence
            """,
            (int(run_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_logs(
    run_id: int,
    *,
    level: str = "DEBUG",
    after_sequence: int = 0,
    limit: int = 1000,
) -> list[dict]:
    thresholds = {
        "DEBUG": ("DEBUG", "INFO", "WARN", "ERROR"),
        "INFO": ("INFO", "WARN", "ERROR"),
        "ERROR": ("ERROR",),
    }
    normalized = str(level).upper()
    if normalized not in thresholds:
        raise ValueError("日志级别必须为 DEBUG、INFO 或 ERROR。")
    accepted = thresholds[normalized]
    placeholders = ",".join("?" for _ in accepted)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM backtest_logs
            WHERE run_id = ?
              AND sequence > ?
              AND level IN ({placeholders})
            ORDER BY sequence
            LIMIT ?
            """,
            (
                int(run_id),
                max(0, int(after_sequence)),
                *accepted,
                max(1, min(int(limit), 5000)),
            ),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["context"] = _decode(item.pop("context_json"), None)
        result.append(item)
    return result


def upsert_corporate_actions(
    actions: list[dict],
    *,
    symbols: list[str],
    coverage_start: str,
    coverage_end: str,
) -> None:
    now = utc_now_iso()
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in symbols)
        stale_rows = (
            conn.execute(
                f"""
                SELECT DISTINCT action.provider_id, action.first_seen_at
                FROM corporate_actions AS action
                LEFT JOIN corporate_action_legs AS leg
                  ON leg.provider_id = action.provider_id
                WHERE (action.symbol IN ({placeholders})
                       OR leg.symbol IN ({placeholders}))
                  AND COALESCE(
                        action.effective_date,
                        action.ex_date,
                        action.process_date
                      ) >= ?
                  AND COALESCE(
                        action.effective_date,
                        action.ex_date,
                        action.process_date
                      ) <= ?
                """,
                (*symbols, *symbols, coverage_start, coverage_end),
            ).fetchall()
            if symbols
            else []
        )
        stale_ids = [row["provider_id"] for row in stale_rows]
        previous_first_seen = {
            row["provider_id"]: row["first_seen_at"]
            for row in stale_rows
            if row["first_seen_at"]
        }
        if stale_ids:
            stale_placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"DELETE FROM corporate_action_legs WHERE provider_id IN ({stale_placeholders})",
                stale_ids,
            )
            conn.execute(
                f"DELETE FROM corporate_actions WHERE provider_id IN ({stale_placeholders})",
                stale_ids,
            )
        conn.executemany(
            """
            INSERT INTO corporate_actions (
                provider_id, provider, action_type, symbol, process_date,
                ex_date, record_date, payable_date, old_rate, new_rate,
                cash_rate, currency, region, sub_type, special, foreign_flag,
                due_bill_on_date, due_bill_off_date, effective_date,
                event_status, instrument_key, identity_status,
                first_seen_at, last_seen_at, payload_json, synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                provider = excluded.provider,
                action_type = excluded.action_type,
                symbol = excluded.symbol,
                process_date = excluded.process_date,
                ex_date = excluded.ex_date,
                record_date = excluded.record_date,
                payable_date = excluded.payable_date,
                old_rate = excluded.old_rate,
                new_rate = excluded.new_rate,
                cash_rate = excluded.cash_rate,
                currency = excluded.currency,
                region = excluded.region,
                sub_type = excluded.sub_type,
                special = excluded.special,
                foreign_flag = excluded.foreign_flag,
                due_bill_on_date = excluded.due_bill_on_date,
                due_bill_off_date = excluded.due_bill_off_date,
                effective_date = excluded.effective_date,
                event_status = excluded.event_status,
                instrument_key = excluded.instrument_key,
                identity_status = excluded.identity_status,
                last_seen_at = excluded.last_seen_at,
                payload_json = excluded.payload_json,
                synced_at = excluded.synced_at
            """,
            [
                (
                    action["provider_id"],
                    action.get("provider", "alpaca"),
                    action["action_type"],
                    action["symbol"],
                    action["process_date"],
                    action.get("ex_date"),
                    action.get("record_date"),
                    action.get("payable_date"),
                    action.get("old_rate"),
                    action.get("new_rate"),
                    action.get("cash_rate"),
                    action.get("currency"),
                    action.get("region"),
                    action.get("sub_type"),
                    1 if action.get("special") else 0,
                    1 if action.get("foreign") else 0,
                    action.get("due_bill_on_date"),
                    action.get("due_bill_off_date"),
                    action.get("effective_date") or action.get("ex_date") or action["process_date"],
                    action.get("event_status", "active"),
                    next(
                        (
                            f"cusip:{leg['cusip']}"
                            for leg in action.get("legs", [])
                            if leg.get("cusip")
                        ),
                        None,
                    ),
                    "identifier" if any(
                        leg.get("cusip") or leg.get("isin")
                        for leg in action.get("legs", [])
                    ) else "unresolved",
                    previous_first_seen.get(action["provider_id"], now),
                    now,
                    _json(action.get("payload", {})),
                    now,
                )
                for action in actions
            ],
        )
        conn.executemany(
            """
            INSERT INTO corporate_action_legs (
                provider_id, role, instrument_key, symbol, cusip, isin,
                share_rate, cash_rate, currency, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    action["provider_id"],
                    leg["role"],
                    (
                        f"cusip:{leg['cusip']}" if leg.get("cusip")
                        else f"isin:{leg['isin']}" if leg.get("isin")
                        else None
                    ),
                    leg.get("symbol"),
                    leg.get("cusip"),
                    leg.get("isin"),
                    leg.get("share_rate"),
                    leg.get("cash_rate"),
                    leg.get("currency"),
                    _json(leg),
                )
                for action in actions
                for leg in action.get("legs", [])
            ],
        )
        for action in actions:
            effective_date = (
                action.get("effective_date")
                or action.get("ex_date")
                or action["process_date"]
            )
            for leg in action.get("legs", []):
                instrument_key = (
                    f"cusip:{leg['cusip']}" if leg.get("cusip")
                    else f"isin:{leg['isin']}" if leg.get("isin")
                    else None
                )
                if not instrument_key:
                    continue
                role = leg.get("role") or "subject"
                valid_from = effective_date if role == "target" else None
                valid_to = (
                    effective_date
                    if role in {"source", "acquiree"}
                    and action["action_type"] == "name_change"
                    else None
                )
                if leg.get("symbol"):
                    conn.execute(
                        """
                        INSERT INTO instrument_symbols (
                            instrument_key, symbol, valid_from, valid_to,
                            is_primary, source, confidence, created_at, updated_at
                        )
                        SELECT ?, ?, ?, ?, ?, 'alpaca_corporate_actions',
                               'provider', ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM instrument_symbols
                            WHERE instrument_key = ? AND symbol = ?
                              AND COALESCE(valid_from, '') = COALESCE(?, '')
                        )
                        """,
                        (
                            instrument_key,
                            leg["symbol"],
                            valid_from,
                            valid_to,
                            1 if role in {"subject", "target"} else 0,
                            now,
                            now,
                            instrument_key,
                            leg["symbol"],
                            valid_from,
                        ),
                    )
                    if valid_to:
                        conn.execute(
                            """
                            UPDATE instrument_symbols
                            SET valid_to = ?, updated_at = ?
                            WHERE instrument_key = ? AND symbol = ?
                              AND COALESCE(valid_from, '') = COALESCE(?, '')
                            """,
                            (valid_to, now, instrument_key, leg["symbol"], valid_from),
                        )
                    if role in {"subject", "target"}:
                        conn.execute(
                            """
                            UPDATE symbols
                            SET cusip = COALESCE(?, cusip),
                                isin = COALESCE(?, isin),
                                updated_at = ?
                            WHERE symbol = ?
                            """,
                            (leg.get("cusip"), leg.get("isin"), now, leg["symbol"]),
                        )
                for identifier_type in ("cusip", "isin"):
                    identifier_value = leg.get(identifier_type)
                    if not identifier_value:
                        continue
                    conn.execute(
                        """
                        INSERT INTO instrument_identifiers (
                            instrument_key, identifier_type, identifier_value,
                            valid_from, valid_to, source, confidence,
                            created_at, updated_at
                        )
                        SELECT ?, ?, ?, ?, ?, 'alpaca_corporate_actions',
                               'provider', ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM instrument_identifiers
                            WHERE identifier_type = ? AND identifier_value = ?
                              AND COALESCE(valid_from, '') = COALESCE(?, '')
                        )
                        """,
                        (
                            instrument_key,
                            identifier_type,
                            identifier_value,
                            valid_from,
                            valid_to,
                            now,
                            now,
                            identifier_type,
                            identifier_value,
                            valid_from,
                        ),
                    )
        conn.executemany(
            """
            INSERT INTO corporate_action_sync_state (
                symbol, coverage_start, coverage_end, status, last_error, synced_at
            )
            VALUES (?, ?, ?, 'success', NULL, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                coverage_start = MIN(
                    corporate_action_sync_state.coverage_start,
                    excluded.coverage_start
                ),
                coverage_end = MAX(
                    corporate_action_sync_state.coverage_end,
                    excluded.coverage_end
                ),
                status = 'success',
                last_error = NULL,
                synced_at = excluded.synced_at
            """,
            [
                (symbol, coverage_start, coverage_end, now)
                for symbol in symbols
            ],
        )


def mark_corporate_action_sync_error(
    symbols: list[str],
    *,
    coverage_start: str,
    coverage_end: str,
    error: str,
) -> None:
    now = utc_now_iso()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO corporate_action_sync_state (
                symbol, coverage_start, coverage_end, status, last_error, synced_at
            )
            VALUES (?, ?, ?, 'error', ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                status = CASE
                    WHEN corporate_action_sync_state.status = 'success'
                    THEN 'success'
                    ELSE 'error'
                END,
                last_error = excluded.last_error,
                synced_at = excluded.synced_at
            """,
            [
                (symbol, coverage_start, coverage_end, error, now)
                for symbol in symbols
            ],
        )


def corporate_action_coverage(symbol: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM corporate_action_sync_state
            WHERE symbol = ?
            """,
            (str(symbol).upper(),),
        ).fetchone()
    return dict(row) if row else None


def get_corporate_actions(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
) -> list[dict]:
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT action.*
            FROM corporate_actions AS action
            LEFT JOIN corporate_action_legs AS leg
              ON leg.provider_id = action.provider_id
            WHERE (action.symbol IN ({placeholders})
                   OR leg.symbol IN ({placeholders}))
              AND COALESCE(action.effective_date, action.ex_date, action.process_date) >= ?
              AND COALESCE(action.effective_date, action.ex_date, action.process_date) <= ?
            ORDER BY COALESCE(action.effective_date, action.ex_date, action.process_date),
                     action.provider_id
            """,
            (*normalized, *normalized, start_date, end_date),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = _decode(item.pop("payload_json"), {})
        with get_connection() as leg_conn:
            leg_rows = leg_conn.execute(
                """
                SELECT role, instrument_key, symbol, cusip, isin,
                       share_rate, cash_rate, currency, payload_json
                FROM corporate_action_legs
                WHERE provider_id = ?
                ORDER BY id
                """,
                (item["provider_id"],),
            ).fetchall()
        item["legs"] = []
        for leg_row in leg_rows:
            leg = dict(leg_row)
            payload = _decode(leg.pop("payload_json"), {})
            item["legs"].append({**payload, **leg})
        result.append(item)
    return result
