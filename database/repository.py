from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re
import sqlite3

from config import MAX_DB_PAGE_SIZE
from database.db import get_connection


DEFAULT_CHART_VIEWS = [
    {"view_code": "1m", "period_type": "minute", "period_value": 1, "name": "1分钟K"},
    {"view_code": "15m", "period_type": "minute", "period_value": 15, "name": "15分钟K"},
    {"view_code": "1h", "period_type": "minute", "period_value": 60, "name": "1小时K"},
    {"view_code": "4h", "period_type": "minute", "period_value": 240, "name": "4小时K"},
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

DEFAULT_SYMBOL_ALIASES = [
    {
        "common_symbol": "USDINDEX",
        "display_name": "USDindex",
        "yahoo_symbol": "DX-Y.NYB",
        "twelvedata_symbol": None,
        "notes": "US Dollar Index. Twelve Data 当前账号未验证到可用代码。",
    },
    {
        "common_symbol": "XAU/USD",
        "display_name": "XAU/USD",
        "yahoo_symbol": None,
        "twelvedata_symbol": "XAU/USD",
        "notes": "现货黄金。Yahoo Finance 的 XAUUSD=X 当前 chart 接口无数据；GC=F 是黄金期货代理。",
    },
    {
        "common_symbol": "GOLDFUTURES",
        "display_name": "GoldFutures",
        "yahoo_symbol": "GC=F",
        "twelvedata_symbol": None,
        "notes": "COMEX 黄金期货，可作为现货黄金代理。",
    },
    {
        "common_symbol": "US2Y",
        "display_name": "US2Y",
        "yahoo_symbol": None,
        "twelvedata_symbol": "US2Y",
        "notes": "US Treasury Yield 2 Years. Yahoo Finance 的 ^UST2Y 当前 chart 接口无数据。",
    },
    {
        "common_symbol": "US10Y",
        "display_name": "US10Y",
        "yahoo_symbol": "^TNX",
        "twelvedata_symbol": None,
        "notes": "CBOE Interest Rate 10 Year Treasury Note. Twelve Data 当前账号未验证到可用代码。",
    },
    {
        "common_symbol": "SPX",
        "display_name": "SPX",
        "yahoo_symbol": "^GSPC",
        "twelvedata_symbol": "SPX",
        "notes": "S&P 500. Twelve Data 代码存在但当前套餐不可用。",
    },
    {
        "common_symbol": "IXIC",
        "display_name": "IXIC",
        "yahoo_symbol": "^IXIC",
        "twelvedata_symbol": None,
        "notes": "NASDAQ Composite. Twelve Data 当前账号未验证到可用代码。",
    },
    {
        "common_symbol": "NDX",
        "display_name": "NDX",
        "yahoo_symbol": "^NDX",
        "twelvedata_symbol": "NDX",
        "notes": "NASDAQ-100. Twelve Data 代码存在但当前套餐不可用。",
    },
    {
        "common_symbol": "DJI",
        "display_name": "DJI",
        "yahoo_symbol": "^DJI",
        "twelvedata_symbol": None,
        "notes": "Dow Jones Industrial Average. Twelve Data 当前账号未验证到可用指数代码。",
    },
    {
        "common_symbol": "RUT",
        "display_name": "RUT",
        "yahoo_symbol": "^RUT",
        "twelvedata_symbol": None,
        "notes": "Russell 2000. Twelve Data 当前账号未验证到可用代码。",
    },
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_params(params: dict) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_symbol_key(symbol: str | None) -> str:
    return (symbol or "").strip().upper()


def seed_symbol_aliases() -> None:
    now = utc_now_iso()
    with get_connection() as conn:
        for item in DEFAULT_SYMBOL_ALIASES:
            conn.execute(
                """
                INSERT INTO symbol_aliases
                    (common_symbol, display_name, yahoo_symbol, twelvedata_symbol, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(common_symbol) DO UPDATE SET
                    display_name = excluded.display_name,
                    yahoo_symbol = excluded.yahoo_symbol,
                    twelvedata_symbol = excluded.twelvedata_symbol,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    item["common_symbol"],
                    item["display_name"],
                    item["yahoo_symbol"],
                    item["twelvedata_symbol"],
                    item["notes"],
                    now,
                    now,
                ),
            )


def resolve_symbol_alias(symbol: str) -> dict:
    normalized = normalize_symbol_key(symbol)
    if not normalized:
        raise ValueError("Symbol is required")

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT common_symbol, display_name, yahoo_symbol, twelvedata_symbol, notes
            FROM symbol_aliases
            WHERE UPPER(common_symbol) = ?
               OR UPPER(display_name) = ?
               OR UPPER(COALESCE(yahoo_symbol, '')) = ?
               OR UPPER(COALESCE(twelvedata_symbol, '')) = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (normalized, normalized, normalized, normalized),
        ).fetchone()

    if row:
        return dict(row)

    return {
        "common_symbol": normalized,
        "display_name": normalized,
        "yahoo_symbol": normalized,
        "twelvedata_symbol": normalized,
        "notes": None,
    }


def get_symbol_display_name(symbol: str) -> str:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT display_name
            FROM symbol_aliases
            WHERE UPPER(common_symbol) = ?
            LIMIT 1
            """,
            (normalize_symbol_key(symbol),),
        ).fetchone()
    return row["display_name"] if row else symbol


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
                (symbol, name, exchange_name, currency, show_weekend_data,
                 show_in_overview, display_order, asset_class, quantity_step,
                 alpaca_asset_id, cusip, isin,
                 history_start_date, history_start_source,
                 history_start_verified, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name = COALESCE(excluded.name, symbols.name),
                exchange_name = COALESCE(excluded.exchange_name, symbols.exchange_name),
                currency = COALESCE(excluded.currency, symbols.currency),
                asset_class = CASE
                    WHEN ? = 1 THEN excluded.asset_class
                    ELSE symbols.asset_class
                END,
                quantity_step = COALESCE(excluded.quantity_step, symbols.quantity_step),
                alpaca_asset_id = COALESCE(excluded.alpaca_asset_id, symbols.alpaca_asset_id),
                cusip = COALESCE(excluded.cusip, symbols.cusip),
                isin = COALESCE(excluded.isin, symbols.isin),
                history_start_date = COALESCE(
                    excluded.history_start_date,
                    symbols.history_start_date
                ),
                history_start_source = COALESCE(
                    excluded.history_start_source,
                    symbols.history_start_source
                ),
                history_start_verified = MAX(
                    excluded.history_start_verified,
                    symbols.history_start_verified
                ),
                updated_at = excluded.updated_at
            """,
            (
                symbol,
                metadata.get("name"),
                metadata.get("exchange"),
                metadata.get("currency"),
                next_order,
                metadata.get("asset_class") or "us_equity",
                metadata.get("quantity_step"),
                metadata.get("alpaca_asset_id"),
                metadata.get("cusip"),
                metadata.get("isin"),
                metadata.get("history_start_date"),
                metadata.get("history_start_source"),
                1 if metadata.get("history_start_verified") else 0,
                now,
                now,
                1 if metadata.get("asset_class") else 0,
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
            SELECT id, symbol, name, exchange_name, currency,
                   show_weekend_data, show_in_overview,
                   alpaca_symbol, alpaca_asset_id, alpaca_supported,
                   alpaca_checked_at, alpaca_error, asset_class, cusip, isin,
                   quantity_step, history_start_date,
                   history_start_source, history_start_verified,
                   created_at, updated_at
            FROM symbols
            WHERE id = ?
            """,
            (symbol_id,),
        ).fetchone()
    data = dict(row)
    data["show_weekend_data"] = bool(data["show_weekend_data"])
    data["show_in_overview"] = bool(data["show_in_overview"])
    data["alpaca_supported"] = (
        bool(data["alpaca_supported"])
        if data["alpaca_supported"] is not None
        else None
    )
    data["history_start_verified"] = bool(data["history_start_verified"])
    data["display_symbol"] = get_symbol_display_name(data["symbol"])
    return data


def mark_symbol_history_start(
    symbol: str,
    history_start_date: str,
    *,
    source: str,
    verified: bool = True,
    asset_class: str | None = None,
    quantity_step: float | None = None,
) -> dict:
    normalized = normalize_symbol_key(symbol)
    upsert_symbol(normalized)
    date.fromisoformat(str(history_start_date))
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE symbols
            SET history_start_date = CASE
                    WHEN history_start_date IS NULL
                      OR ? < history_start_date
                    THEN ?
                    ELSE history_start_date
                END,
                history_start_source = CASE
                    WHEN history_start_date IS NULL
                      OR ? <= history_start_date
                    THEN ?
                    ELSE history_start_source
                END,
                history_start_verified = ?,
                asset_class = COALESCE(?, asset_class),
                quantity_step = COALESCE(?, quantity_step),
                updated_at = ?
            WHERE symbol = ?
            """,
            (
                history_start_date,
                history_start_date,
                history_start_date,
                source,
                1 if verified else 0,
                asset_class,
                quantity_step,
                now,
                normalized,
            ),
        )
    return get_symbol(normalized)


def set_alpaca_capability(
    symbol: str,
    *,
    supported: bool,
    alpaca_symbol: str | None = None,
    alpaca_asset_id: str | None = None,
    error: str | None = None,
) -> dict:
    normalized = normalize_symbol_key(symbol)
    upsert_symbol(normalized)
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE symbols
            SET alpaca_symbol = ?,
                alpaca_asset_id = COALESCE(?, alpaca_asset_id),
                alpaca_supported = ?,
                alpaca_checked_at = ?,
                alpaca_error = ?,
                updated_at = ?
            WHERE symbol = ?
            """,
            (
                normalize_symbol_key(alpaca_symbol or normalized),
                alpaca_asset_id,
                1 if supported else 0,
                now,
                error,
                now,
                normalized,
            ),
        )
    return get_symbol(normalized)


def get_symbol_price_snapshot(symbol: str) -> dict:
    year_start = f"{date.today().year}-01-01"
    with get_connection() as conn:
        latest_rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume, COALESCE(updated_at, created_at) AS price_updated_at
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
        latest_weekday_row = conn.execute(
            """
            SELECT date
            FROM daily_prices
            WHERE symbol = ? AND strftime('%w', date) NOT IN ('0', '6')
            ORDER BY date DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

    latest = dict(latest_rows[0]) if latest_rows else None
    previous = dict(latest_rows[1]) if len(latest_rows) > 1 else None
    latest_price = latest["close"] if latest else None
    previous_close = previous["close"] if previous else None

    def change_from(days: int) -> tuple[str | None, float | None, float | None]:
        if not latest or latest_price is None:
            return None, None, None

        base_date = datetime.strptime(latest["date"], "%Y-%m-%d").date() - timedelta(days=days)
        with get_connection() as conn:
            base_row = conn.execute(
                """
                SELECT date, close
                FROM daily_prices
                WHERE symbol = ? AND date <= ?
                ORDER BY date DESC
                LIMIT 1
                """,
                (symbol, base_date.isoformat()),
            ).fetchone()

        base_price = base_row["close"] if base_row else None
        if base_price in {None, 0}:
            return (base_row["date"] if base_row else None), base_price, None
        return (
            base_row["date"],
            base_price,
            (latest_price - base_price) / base_price * 100,
        )

    daily_change = None
    daily_change_percent = None
    if latest_price is not None and previous_close not in {None, 0}:
        daily_change = latest_price - previous_close
        daily_change_percent = daily_change / previous_close * 100

    weekly_base_date, weekly_base_price, weekly_percent = change_from(7)
    monthly_base_date, monthly_base_price, monthly_percent = change_from(30)

    ytd_base = ytd_row["close"] if ytd_row else None
    ytd_percent = None
    if latest_price is not None and ytd_base not in {None, 0}:
        ytd_percent = (latest_price - ytd_base) / ytd_base * 100

    return {
        "latest_date": latest["date"] if latest else None,
        "latest_weekday_date": (
            latest_weekday_row["date"]
            if latest_weekday_row
            else None
        ),
        "latest_price_updated_at": latest["price_updated_at"] if latest else None,
        "latest_price": latest_price,
        "previous_close": previous_close,
        "daily_change": daily_change,
        "daily_change_percent": daily_change_percent,
        "weekly_base_date": weekly_base_date,
        "weekly_base_price": weekly_base_price,
        "weekly_percent": weekly_percent,
        "monthly_base_date": monthly_base_date,
        "monthly_base_price": monthly_base_price,
        "monthly_percent": monthly_percent,
        "ytd_base_date": ytd_row["date"] if ytd_row else None,
        "ytd_base_price": ytd_base,
        "ytd_percent": ytd_percent,
    }


def list_market_overview(page: int = 1, page_size: int = 100) -> dict:
    with get_connection() as conn:
        total_rows = conn.execute(
            "SELECT COUNT(*) AS count FROM symbols WHERE show_in_overview = 1"
        ).fetchone()["count"]
        rows = conn.execute(
            """
            SELECT id, symbol, name, show_weekend_data,
                   display_order, updated_at
            FROM symbols
            WHERE show_in_overview = 1
            ORDER BY display_order ASC, id ASC
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["show_weekend_data"] = bool(item["show_weekend_data"])
        item["display_symbol"] = get_symbol_display_name(item["symbol"])
        item.update(get_symbol_price_snapshot(item["symbol"]))
        item["analysis_latest_date"] = (
            item["latest_date"]
            if item["show_weekend_data"]
            else item["latest_weekday_date"]
        )
        items.append(item)

    return {
        "items": items,
        "page": 1,
        "page_size": total_rows,
        "total_rows": total_rows,
        "total_pages": 1,
    }


def save_trendline_analysis_snapshot(
        symbol: str,
        payload: dict,
        summary: dict,
        algorithm_version: str,
) -> dict:
    normalized = normalize_symbol_key(symbol)
    computed_at = utc_now_iso()
    period = str(payload.get("period") or "1D")
    window_size = int(payload.get("requested_window_size") or 150)
    show_weekend_data = 1 if payload.get("show_weekend_data") else 0
    fingerprint = str(payload.get("data_fingerprint") or "")
    latest_data_date = payload.get("latest_data_date")
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO trendline_analysis_snapshots (
                symbol, period, window_size, show_weekend_data,
                algorithm_version, latest_data_date, data_fingerprint,
                payload_json, summary_json, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                symbol, period, window_size, show_weekend_data,
                algorithm_version, data_fingerprint
            ) DO UPDATE SET
                latest_data_date = excluded.latest_data_date,
                payload_json = excluded.payload_json,
                summary_json = excluded.summary_json,
                computed_at = excluded.computed_at
            """,
            (
                normalized,
                period,
                window_size,
                show_weekend_data,
                algorithm_version,
                latest_data_date,
                fingerprint,
                payload_json,
                summary_json,
                computed_at,
            ),
        )
    return {
        **summary,
        "computed_at": computed_at,
        "algorithm_version": algorithm_version,
    }


def list_latest_trendline_analysis_snapshots(
        algorithm_version: str,
        period: str = "1D",
        window_size: int = 150,
) -> dict[str, dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, show_weekend_data, latest_data_date,
                   payload_json, summary_json, computed_at
            FROM trendline_analysis_snapshots
            WHERE algorithm_version = ? AND period = ? AND window_size = ?
            ORDER BY computed_at DESC, id DESC
            """,
            (algorithm_version, period, window_size),
        ).fetchall()

    latest: dict[str, dict] = {}
    for row in rows:
        symbol = row["symbol"]
        if symbol in latest:
            continue
        latest[symbol] = {
            "symbol": symbol,
            "show_weekend_data": bool(row["show_weekend_data"]),
            "latest_data_date": row["latest_data_date"],
            "payload": json.loads(row["payload_json"]),
            "summary": json.loads(row["summary_json"]),
            "computed_at": row["computed_at"],
        }
    return latest


def get_latest_trendline_analysis_snapshot(
        symbol: str,
        algorithm_version: str,
        period: str = "1D",
        window_size: int = 150,
) -> dict | None:
    normalized = normalize_symbol_key(symbol)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT symbol, show_weekend_data, latest_data_date,
                   payload_json, summary_json, computed_at
            FROM trendline_analysis_snapshots
            WHERE symbol = ? AND algorithm_version = ?
              AND period = ? AND window_size = ?
            ORDER BY computed_at DESC, id DESC
            LIMIT 1
            """,
            (normalized, algorithm_version, period, window_size),
        ).fetchone()
    if not row:
        return None
    return {
        "symbol": row["symbol"],
        "show_weekend_data": bool(row["show_weekend_data"]),
        "latest_data_date": row["latest_data_date"],
        "payload": json.loads(row["payload_json"]),
        "summary": json.loads(row["summary_json"]),
        "computed_at": row["computed_at"],
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
            WHERE show_in_overview = 1
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


def list_overview_symbols() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.symbol, a.id AS alias_id, a.display_name, a.yahoo_symbol, a.twelvedata_symbol
            FROM symbols s
            LEFT JOIN symbol_aliases a ON UPPER(a.common_symbol) = UPPER(s.symbol)
            WHERE s.show_in_overview = 1
            ORDER BY s.display_order ASC, s.id ASC
            """
        ).fetchall()

    result = []
    for row in rows:
        data = dict(row)
        data["common_symbol"] = data.pop("symbol")
        data["display_name"] = data["display_name"] or data["common_symbol"]
        if data.pop("alias_id") is None:
            data["yahoo_symbol"] = data["common_symbol"]
            data["twelvedata_symbol"] = data["common_symbol"]
        result.append(data)
    return result


def set_symbol_overview_visibility(symbol: str, show_in_overview: bool) -> dict:
    normalized = normalize_symbol_key(symbol)
    if not normalized:
        raise ValueError("Symbol is required")

    now = utc_now_iso()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT symbol
            FROM symbols
            WHERE UPPER(symbol) = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if not row:
            raise ValueError("Unknown symbol")
        conn.execute(
            """
            UPDATE symbols
            SET show_in_overview = ?, updated_at = ?
            WHERE symbol = ?
            """,
            (1 if show_in_overview else 0, now, row["symbol"]),
        )
    return get_symbol(row["symbol"])


def update_symbol_settings(symbol: str, payload: dict) -> dict:
    symbol_id = upsert_symbol(symbol)
    allowed: dict[str, object] = {}
    if "show_weekend_data" in payload:
        allowed["show_weekend_data"] = 1 if bool(payload["show_weekend_data"]) else 0
    if "show_in_overview" in payload:
        allowed["show_in_overview"] = 1 if bool(payload["show_in_overview"]) else 0

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
    normalized_code = (view_code or "").strip()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM symbol_chart_views
            WHERE symbol = ? AND view_code = ?
            """,
            (symbol, normalized_code),
        ).fetchone()
    if not row:
        match = re.fullmatch(r"([1-9]\d{0,2})(m|h|D)", normalized_code)
        if not match:
            raise ValueError("Unknown chart view")
        value = int(match.group(1))
        suffix = match.group(2)
        if suffix == "m":
            period_type, period_value, name = "minute", value, f"{value}分钟K"
        elif suffix == "h":
            period_type, period_value, name = "minute", value * 60, f"{value}小时K"
        else:
            period_type, period_value, name = "day", value, f"{value}日K"
        if period_type == "minute" and period_value > 390:
            raise ValueError("Unknown chart view")
        symbol_id = upsert_symbol(symbol)
        now = utc_now_iso()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO symbol_chart_views (
                    symbol_id, symbol, view_code, period_type,
                    period_value, name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol_id, view_code) DO NOTHING
                """,
                (
                    symbol_id,
                    symbol,
                    normalized_code,
                    period_type,
                    period_value,
                    name,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM symbol_chart_views
                WHERE symbol_id = ? AND view_code = ?
                """,
                (symbol_id, normalized_code),
            ).fetchone()
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
        {"code": "EMA8", "name": "EMA8", "indicator_type": "EMA", "params": {"period": 8}, "description": "指数移动平均"},
        {"code": "EMA13", "name": "EMA13", "indicator_type": "EMA", "params": {"period": 13}, "description": "指数移动平均"},
        {"code": "MA20", "name": "MA20", "indicator_type": "MA", "params": {"period": 20}, "description": "简单移动平均"},
        {"code": "ATR14", "name": "ATR14", "indicator_type": "ATR", "params": {"period": 14}, "description": "Wilder 绝对 ATR"},
        {"code": "RATR14", "name": "相对ATR14", "indicator_type": "RATR", "params": {"period": 14}, "description": "(当前收盘价 - 14 个交易日前收盘价) / 前一日 Wilder ATR(14)"},
        {"code": "WTME40H15E1e-08", "name": "WTME40(h=15)", "indicator_type": "WTME", "params": {"period": 40, "half_life": 15.0, "epsilon": 1e-8}, "description": "加权真实波幅动量效率"},
    ]
    now = utc_now_iso()
    with get_connection() as conn:
        for item in defaults:
            conn.execute(
                """
                INSERT INTO indicators
                    (code, name, indicator_type, params_json, is_favorite, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    item["code"],
                    item["name"],
                    item["indicator_type"],
                    normalize_params(item["params"]),
                    item["description"],
                    now,
                    now,
                ),
            )


def validate_indicator(indicator_type: str, params: dict) -> tuple[str, dict]:
    normalized_type = str(indicator_type or "").strip().upper()
    if normalized_type not in {"MA", "EMA", "ATR", "RATR", "WTME"}:
        raise ValueError("仅支持 MA、EMA、ATR、相对 ATR 和 WTME 指标。")

    try:
        period = int(params.get("period"))
    except (TypeError, ValueError) as exc:
        raise ValueError("指标周期必须是整数。") from exc

    if period < 2 or period > 500:
        raise ValueError("指标周期必须在 2 到 500 之间。")

    if normalized_type != "WTME":
        return normalized_type, {"period": period}

    try:
        half_life = float(params.get("half_life", 15))
        epsilon = float(params.get("epsilon", 1e-8))
    except (TypeError, ValueError) as exc:
        raise ValueError("WTME 半衰期和 epsilon 必须是数值。") from exc
    if not math.isfinite(half_life) or half_life < 0.1 or half_life > 500:
        raise ValueError("WTME 半衰期必须在 0.1 到 500 之间。")
    if not math.isfinite(epsilon) or epsilon < 1e-12 or epsilon > 0.01:
        raise ValueError("WTME epsilon 必须在 1e-12 到 0.01 之间。")
    return normalized_type, {
        "period": period,
        "half_life": half_life,
        "epsilon": epsilon,
    }


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
    if normalized_type == "RATR":
        default_name = f"相对ATR{normalized_params['period']}"
    elif normalized_type == "WTME":
        default_name = (
            f"WTME{normalized_params['period']}"
            f"(h={normalized_params['half_life']:g})"
        )
    else:
        default_name = f"{normalized_type}{normalized_params['period']}"
    code = f"{normalized_type}{normalized_params['period']}"
    if normalized_type == "WTME":
        half_life_code = format(normalized_params["half_life"], ".15g")
        epsilon_code = format(normalized_params["epsilon"], ".15g")
        code += f"H{half_life_code}E{epsilon_code}"
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
                (
                    "Wilder 绝对 ATR"
                    if normalized_type == "ATR"
                    else "相对 ATR 动量评分（当前价差 / 前一日 Wilder ATR）"
                    if normalized_type == "RATR"
                    else "加权方向收益 / 加权标准化真实波幅 × 100"
                    if normalized_type == "WTME"
                    else "用户创建指标"
                ),
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


def upsert_daily_prices(
    symbol: str,
    rows: list[dict],
    *,
    source_provider: str | None = None,
    source_timeframe: str | None = None,
) -> int:
    now = utc_now_iso()
    def price_basis(row: dict) -> str:
        explicit = str(row.get("price_basis") or "").strip().lower()
        if explicit in {"raw", "split_adjusted", "total_return_adjusted", "unknown"}:
            return explicit
        provider = str(row.get("source_provider") or source_provider or "").lower()
        return "raw" if provider == "alpaca" else "unknown"

    payload = [
        (
            symbol,
            row["date"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row.get("volume", 0),
            row.get("source_provider", source_provider),
            row.get("source_timeframe", source_timeframe),
            price_basis(row),
            1 if row.get("is_complete", True) else 0,
            now,
            now,
        )
        for row in rows
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO daily_prices
                (symbol, date, open, high, low, close, volume,
                 source_provider, source_timeframe, price_basis,
                 is_complete, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                source_provider = excluded.source_provider,
                source_timeframe = excluded.source_timeframe,
                price_basis = excluded.price_basis,
                is_complete = excluded.is_complete,
                updated_at = excluded.updated_at
            """,
            payload,
        )
    if payload:
        mark_symbol_history_start(
            symbol,
            min(str(row[1]) for row in payload),
            source=source_provider or str(rows[0].get("source_provider") or "database"),
            verified=True,
        )
    return len(payload)


def delete_daily_prices(
    symbol: str,
    dates: list[str],
    *,
    source_timeframe: str | None = None,
) -> int:
    """Remove explicitly identified derived rows; raw minute data is untouched."""
    normalized_dates = sorted({date.fromisoformat(value).isoformat() for value in dates})
    if not normalized_dates:
        return 0
    with get_connection() as conn:
        before = conn.total_changes
        if source_timeframe is None:
            conn.executemany(
                "DELETE FROM daily_prices WHERE symbol = ? AND date = ?",
                [(symbol, value) for value in normalized_dates],
            )
        else:
            conn.executemany(
                """
                DELETE FROM daily_prices
                WHERE symbol = ? AND date = ? AND source_timeframe = ?
                """,
                [(symbol, value, source_timeframe) for value in normalized_dates],
            )
        return conn.total_changes - before


def get_daily_prices(
    symbol: str,
    start_date: str | None = None,
    *,
    include_metadata: bool = False,
) -> list[dict]:
    params: list[str] = [symbol]
    where = "symbol = ?"
    if start_date:
        where += " AND date >= ?"
        params.append(start_date)
    fields = "date, open, high, low, close, volume"
    if include_metadata:
        fields += ", source_provider, source_timeframe, price_basis, is_complete, updated_at"

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {fields}
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
                    WHEN is_complete = 0
                      OR date(COALESCE(updated_at, created_at)) = date
                    THEN NULL
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
