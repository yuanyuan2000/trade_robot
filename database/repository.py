from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import sqlite3

from config import MAX_DB_PAGE_SIZE
from database.db import get_connection


DEFAULT_CHART_VIEWS = [
    {"view_code": "1D", "period_type": "day", "period_value": 1, "name": "日K"},
    {"view_code": "3D", "period_type": "day", "period_value": 3, "name": "3日K"},
    {"view_code": "1W", "period_type": "week", "period_value": 1, "name": "周K"},
    {"view_code": "1M", "period_type": "month", "period_value": 1, "name": "月K"},
]

INDICATOR_COLORS = [
    "#176b87",
    "#8a5a00",
    "#7d3c98",
    "#23745a",
    "#b54747",
    "#4d6f8f",
    "#9a6b32",
    "#5267a8",
    "#16817a",
    "#9b3d58",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_params(params: dict) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def upsert_symbol(symbol: str, metadata: dict | None = None) -> int:
    metadata = metadata or {}
    now = utc_now_iso()
    with get_connection() as conn:
        next_order = conn.execute(
            "SELECT COALESCE(MAX(display_order), 0) + 1 AS next_order FROM symbols"
        ).fetchone()["next_order"]
        conn.execute(
            """
            INSERT INTO symbols
                (symbol, name, exchange_name, currency, show_weekend_data, display_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name = COALESCE(excluded.name, symbols.name),
                exchange_name = COALESCE(excluded.exchange_name, symbols.exchange_name),
                currency = COALESCE(excluded.currency, symbols.currency),
                updated_at = excluded.updated_at
            """,
            (
                symbol,
                metadata.get("name"),
                metadata.get("exchange"),
                metadata.get("currency"),
                next_order,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT id FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
    return int(row["id"])


def ensure_symbol_chart_views(symbol: str) -> list[dict]:
    symbol_id = upsert_symbol(symbol)
    now = utc_now_iso()
    with get_connection() as conn:
        for view in DEFAULT_CHART_VIEWS:
            conn.execute(
                """
                INSERT INTO symbol_chart_views
                    (symbol_id, symbol, view_code, period_type, period_value, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol_id, view_code) DO UPDATE SET
                    symbol = excluded.symbol,
                    period_type = excluded.period_type,
                    period_value = excluded.period_value,
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (
                    symbol_id,
                    symbol,
                    view["view_code"],
                    view["period_type"],
                    view["period_value"],
                    view["name"],
                    now,
                    now,
                ),
            )
    return list_symbol_chart_views(symbol)


def get_symbol(symbol: str) -> dict:
    symbol_id = upsert_symbol(symbol)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, name, exchange_name, currency, show_weekend_data, created_at, updated_at
            FROM symbols
            WHERE id = ?
            """,
            (symbol_id,),
        ).fetchone()
    data = dict(row)
    data["show_weekend_data"] = bool(data["show_weekend_data"])
    return data


def get_symbol_price_snapshot(symbol: str) -> dict:
    year_start = f"{date.today().year}-01-01"
    with get_connection() as conn:
        latest_rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM daily_prices
            WHERE symbol = ?
            ORDER BY date DESC
            LIMIT 2
            """,
            (symbol,),
        ).fetchall()
        ytd_row = conn.execute(
            """
            SELECT date, close
            FROM daily_prices
            WHERE symbol = ? AND date >= ?
            ORDER BY date ASC
            LIMIT 1
            """,
            (symbol, year_start),
        ).fetchone()

    latest = dict(latest_rows[0]) if latest_rows else None
    previous = dict(latest_rows[1]) if len(latest_rows) > 1 else None
    latest_price = latest["close"] if latest else None
    previous_close = previous["close"] if previous else None
    daily_change = None
    daily_change_percent = None
    if latest_price is not None and previous_close not in {None, 0}:
        daily_change = latest_price - previous_close
        daily_change_percent = daily_change / previous_close * 100

    ytd_base = ytd_row["close"] if ytd_row else None
    ytd_percent = None
    if latest_price is not None and ytd_base not in {None, 0}:
        ytd_percent = (latest_price - ytd_base) / ytd_base * 100

    return {
        "latest_date": latest["date"] if latest else None,
        "latest_price": latest_price,
        "previous_close": previous_close,
        "daily_change": daily_change,
        "daily_change_percent": daily_change_percent,
        "ytd_base_date": ytd_row["date"] if ytd_row else None,
        "ytd_base_price": ytd_base,
        "ytd_percent": ytd_percent,
    }


def list_market_overview(page: int = 1, page_size: int = 100) -> dict:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    with get_connection() as conn:
        total_rows = conn.execute("SELECT COUNT(*) AS count FROM symbols").fetchone()["count"]
        total_pages = max(1, math.ceil(total_rows / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = conn.execute(
            """
            SELECT id, symbol, name, display_order, updated_at
            FROM symbols
            ORDER BY display_order ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item.update(get_symbol_price_snapshot(item["symbol"]))
        items.append(item)

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }


def update_symbol_display_order(symbols: list[str]) -> dict:
    normalized_symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    if not normalized_symbols:
        return list_market_overview()

    now = utc_now_iso()
    with get_connection() as conn:
        current_rows = conn.execute(
            """
            SELECT symbol
            FROM symbols
            ORDER BY display_order ASC, id ASC
            """
        ).fetchall()
        current_symbols = [row["symbol"] for row in current_rows]
        known_order = [symbol for symbol in normalized_symbols if symbol in current_symbols]
        if not known_order:
            return list_market_overview()

        current_indexes = [current_symbols.index(symbol) for symbol in known_order]
        insert_at = min(current_indexes)
        remaining_symbols = [symbol for symbol in current_symbols if symbol not in set(known_order)]
        next_symbols = [
            *remaining_symbols[:insert_at],
            *known_order,
            *remaining_symbols[insert_at:],
        ]

        for index, symbol in enumerate(next_symbols, start=1):
            conn.execute(
                """
                UPDATE symbols
                SET display_order = ?, updated_at = ?
                WHERE symbol = ?
                """,
                (index, now, symbol),
            )

    return list_market_overview()


def update_symbol_settings(symbol: str, payload: dict) -> dict:
    symbol_id = upsert_symbol(symbol)
    allowed: dict[str, object] = {}
    if "show_weekend_data" in payload:
        allowed["show_weekend_data"] = 1 if bool(payload["show_weekend_data"]) else 0

    if allowed:
        allowed["updated_at"] = utc_now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in allowed)
        values = list(allowed.values()) + [symbol_id]
        with get_connection() as conn:
            conn.execute(
                f"UPDATE symbols SET {set_clause} WHERE id = ?",
                values,
            )

    return get_symbol(symbol)


def get_chart_view(symbol: str, view_code: str) -> dict:
    ensure_symbol_chart_views(symbol)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM symbol_chart_views
            WHERE symbol = ? AND view_code = ?
            """,
            (symbol, view_code),
        ).fetchone()
    if not row:
        raise ValueError("Unknown chart view")
    return dict(row)


def list_symbol_chart_views(symbol: str) -> list[dict]:
    symbol_id = upsert_symbol(symbol)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol_id, symbol, view_code, period_type, period_value, name
            FROM symbol_chart_views
            WHERE symbol_id = ?
            ORDER BY
                CASE view_code
                    WHEN '1D' THEN 1
                    WHEN '3D' THEN 2
                    WHEN '1W' THEN 3
                    WHEN '1M' THEN 4
                    ELSE 99
                END,
                period_type,
                period_value
            """,
            (symbol_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def seed_default_indicators() -> None:
    defaults = [
        {"code": "EMA8", "name": "EMA8", "indicator_type": "EMA", "params": {"period": 8}},
        {"code": "EMA13", "name": "EMA13", "indicator_type": "EMA", "params": {"period": 13}},
        {"code": "MA20", "name": "MA20", "indicator_type": "MA", "params": {"period": 20}},
    ]
    now = utc_now_iso()
    with get_connection() as conn:
        for item in defaults:
            conn.execute(
                """
                INSERT INTO indicators
                    (code, name, indicator_type, params_json, is_favorite, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    is_favorite = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    item["code"],
                    item["name"],
                    item["indicator_type"],
                    normalize_params(item["params"]),
                    "系统内置常用指标",
                    now,
                    now,
                ),
            )


def validate_indicator(indicator_type: str, params: dict) -> tuple[str, dict]:
    normalized_type = indicator_type.strip().upper()
    if normalized_type not in {"MA", "EMA"}:
        raise ValueError("仅支持 MA 和 EMA 指标。")

    try:
        period = int(params.get("period"))
    except (TypeError, ValueError) as exc:
        raise ValueError("指标周期必须是整数。") from exc

    if period < 2 or period > 500:
        raise ValueError("指标周期必须在 2 到 500 之间。")

    return normalized_type, {"period": period}


def indicator_to_dict(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data["params"] = json.loads(data.pop("params_json"))
    data["is_favorite"] = bool(data["is_favorite"])
    return data


def list_indicators(favorite: bool | None = None) -> list[dict]:
    params: list[int] = []
    where = ""
    if favorite is not None:
        where = "WHERE is_favorite = ?"
        params.append(1 if favorite else 0)

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, code, name, indicator_type, params_json, is_favorite, description
            FROM indicators
            {where}
            ORDER BY is_favorite DESC, indicator_type ASC, name ASC
            """,
            params,
        ).fetchall()
    return [indicator_to_dict(row) for row in rows]


def get_indicator(indicator_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, code, name, indicator_type, params_json, is_favorite, description
            FROM indicators
            WHERE id = ?
            """,
            (indicator_id,),
        ).fetchone()
    if not row:
        raise ValueError("Unknown indicator")
    return indicator_to_dict(row)


def get_or_create_indicator(indicator_type: str, params: dict, name: str | None = None) -> dict:
    normalized_type, normalized_params = validate_indicator(indicator_type, params)
    params_json = normalize_params(normalized_params)
    default_name = f"{normalized_type}{normalized_params['period']}"
    code = default_name.upper()
    now = utc_now_iso()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, code, name, indicator_type, params_json, is_favorite, description
            FROM indicators
            WHERE indicator_type = ? AND params_json = ?
            """,
            (normalized_type, params_json),
        ).fetchone()
        if row:
            return indicator_to_dict(row)

        conn.execute(
            """
            INSERT INTO indicators
                (code, name, indicator_type, params_json, is_favorite, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                code,
                name or default_name,
                normalized_type,
                params_json,
                "用户创建指标",
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT id, code, name, indicator_type, params_json, is_favorite, description
            FROM indicators
            WHERE indicator_type = ? AND params_json = ?
            """,
            (normalized_type, params_json),
        ).fetchone()
    return indicator_to_dict(row)


def update_indicator(indicator_id: int, payload: dict) -> dict:
    allowed: dict[str, object] = {}
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
            raise ValueError("指标名称不能为空。")
        allowed["name"] = name
    if "is_favorite" in payload:
        allowed["is_favorite"] = 1 if bool(payload["is_favorite"]) else 0
    if "description" in payload:
        allowed["description"] = str(payload["description"]).strip() or None

    if not allowed:
        return get_indicator(indicator_id)

    allowed["updated_at"] = utc_now_iso()
    set_clause = ", ".join(f"{key} = ?" for key in allowed)
    values = list(allowed.values()) + [indicator_id]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE indicators SET {set_clause} WHERE id = ?",
            values,
        )
    return get_indicator(indicator_id)


def list_symbol_indicators(symbol: str, view_code: str) -> list[dict]:
    view = get_chart_view(symbol, view_code)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                si.id,
                si.symbol_id,
                si.symbol,
                si.chart_view_id,
                si.view_code,
                si.indicator_id,
                si.color,
                si.visible,
                si.sort_order,
                i.code,
                i.name,
                i.indicator_type,
                i.params_json,
                i.is_favorite
            FROM symbol_indicators si
            JOIN indicators i ON i.id = si.indicator_id
            WHERE si.chart_view_id = ?
            ORDER BY si.sort_order ASC, si.id ASC
            """,
            (view["id"],),
        ).fetchall()

    result = []
    for row in rows:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json"))
        data["visible"] = bool(data["visible"])
        data["is_favorite"] = bool(data["is_favorite"])
        result.append(data)
    return result


def add_symbol_indicator(
    symbol: str,
    view_code: str,
    indicator_id: int | None = None,
    indicator_type: str | None = None,
    params: dict | None = None,
    name: str | None = None,
) -> dict:
    view = get_chart_view(symbol, view_code)
    if indicator_id is None:
        if not indicator_type or params is None:
            raise ValueError("缺少指标参数。")
        indicator = get_or_create_indicator(indicator_type, params, name)
        indicator_id = int(indicator["id"])
    else:
        indicator = get_indicator(indicator_id)

    existing = list_symbol_indicators(symbol, view_code)
    if len(existing) >= 10:
        raise ValueError("每个标的的每个视图最多同时设置 10 个指标。")
    if any(int(item["indicator_id"]) == int(indicator_id) for item in existing):
        raise ValueError("该视图中已经添加了这个指标。")

    color = INDICATOR_COLORS[len(existing) % len(INDICATOR_COLORS)]
    sort_order = len(existing)
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO symbol_indicators
                (symbol_id, symbol, chart_view_id, view_code, indicator_id, color, visible, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                view["symbol_id"],
                symbol,
                view["id"],
                view_code,
                indicator_id,
                color,
                sort_order,
                now,
                now,
            ),
        )
    return {
        "indicator": indicator,
        "symbol_indicators": list_symbol_indicators(symbol, view_code),
    }


def update_symbol_indicator(symbol: str, view_code: str, symbol_indicator_id: int, payload: dict) -> dict:
    view = get_chart_view(symbol, view_code)
    allowed: dict[str, object] = {}
    if "visible" in payload:
        allowed["visible"] = 1 if bool(payload["visible"]) else 0
    if "color" in payload:
        color = str(payload["color"]).strip()
        if not color.startswith("#") or len(color) not in {4, 7}:
            raise ValueError("颜色格式无效。")
        allowed["color"] = color
    if "sort_order" in payload:
        allowed["sort_order"] = int(payload["sort_order"])

    if allowed:
        allowed["updated_at"] = utc_now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in allowed)
        values = list(allowed.values()) + [symbol_indicator_id, view["id"]]
        with get_connection() as conn:
            conn.execute(
                f"""
                UPDATE symbol_indicators
                SET {set_clause}
                WHERE id = ? AND chart_view_id = ?
                """,
                values,
            )

    return {"symbol_indicators": list_symbol_indicators(symbol, view_code)}


def delete_symbol_indicator(symbol: str, view_code: str, symbol_indicator_id: int) -> dict:
    view = get_chart_view(symbol, view_code)
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM symbol_indicators
            WHERE id = ? AND chart_view_id = ?
            """,
            (symbol_indicator_id, view["id"]),
        )
    return {"symbol_indicators": list_symbol_indicators(symbol, view_code)}


def upsert_daily_prices(symbol: str, rows: list[dict]) -> int:
    now = utc_now_iso()
    payload = [
        (
            symbol,
            row["date"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row.get("volume", 0),
            now,
            now,
        )
        for row in rows
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO daily_prices
                (symbol, date, open, high, low, close, volume, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                updated_at = excluded.updated_at
            """,
            payload,
        )
    return len(payload)


def get_daily_prices(symbol: str, start_date: str | None = None) -> list[dict]:
    params: list[str] = [symbol]
    where = "symbol = ?"
    if start_date:
        where += " AND date >= ?"
        params.append(start_date)

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT date, open, high, low, close, volume
            FROM daily_prices
            WHERE {where}
            ORDER BY date ASC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_symbol_date_bounds(symbol: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT MIN(date) AS min_date, MAX(date) AS max_date, COUNT(*) AS row_count
            FROM daily_prices
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
    if not row or row["row_count"] == 0:
        return None
    return dict(row)


def get_symbol_history_coverage(symbol: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                MIN(date) AS min_date,
                MAX(CASE
                    WHEN date(COALESCE(updated_at, created_at)) = date THEN NULL
                    ELSE date
                END) AS max_complete_date,
                MAX(date) AS max_date,
                COUNT(*) AS row_count
            FROM daily_prices
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
    if not row or row["row_count"] == 0:
        return None
    return dict(row)


def log_api_request(
    provider: str,
    status: str,
    symbol: str | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_request_logs
                (provider, symbol, status, error_code, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (provider, symbol, status, error_code, message, utc_now_iso()),
        )


def list_tables() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name ASC
            """
        ).fetchall()
    return [row["name"] for row in rows]


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def get_table_page(table_name: str, page: int, page_size: int, search: str = "") -> dict:
    allowed_tables = set(list_tables())
    if table_name not in allowed_tables:
        raise ValueError("Unknown table")

    page = max(1, page)
    page_size = min(max(1, page_size), MAX_DB_PAGE_SIZE)
    search = search.strip()
    with get_connection() as conn:
        columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
        ]

        where = ""
        query_params: list[str | int] = []
        if search:
            predicates = [
                f"CAST({quote_identifier(column)} AS TEXT) LIKE ?"
                for column in columns
            ]
            where = "WHERE " + " OR ".join(predicates)
            query_params.extend([f"%{search}%"] * len(columns))

        total_rows = conn.execute(
            f"SELECT COUNT(*) AS count FROM {quote_identifier(table_name)} {where}",
            query_params,
        ).fetchone()["count"]
        total_pages = max(1, math.ceil(total_rows / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT * FROM {quote_identifier(table_name)} {where} LIMIT ? OFFSET ?",
            [*query_params, page_size, offset],
        ).fetchall()

    return {
        "table": table_name,
        "search": search,
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "columns": columns,
        "rows": [dict(row) for row in rows],
    }
