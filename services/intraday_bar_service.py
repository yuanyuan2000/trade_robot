from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timezone
import re
from zoneinfo import ZoneInfo

from database import intraday_repository, repository
from services.alpaca_data_client import fetch_asset


NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN_MINUTE = 9 * 60 + 30
REGULAR_CLOSE_MINUTE = 16 * 60
MAX_CHART_BARS = 5_000
_CUSTOM_PERIOD = re.compile(r"^([1-9]\d{0,2})(m|h|D)$")


def parse_bar_spec(period: str) -> dict:
    value = (period or "1D").strip()
    predefined = {
        "1m": ("minute", 1),
        "15m": ("minute", 15),
        "1h": ("minute", 60),
        "4h": ("minute", 240),
        "1D": ("day", 1),
        "3D": ("day", 3),
        "1W": ("week", 1),
        "1M": ("month", 1),
    }
    if value in predefined:
        unit, size = predefined[value]
    else:
        match = _CUSTOM_PERIOD.fullmatch(value)
        if not match:
            raise ValueError("不支持的K线周期。可使用自定义分钟数、小时数或天数。")
        number = int(match.group(1))
        suffix = match.group(2)
        if suffix == "m":
            unit, size = "minute", number
        elif suffix == "h":
            unit, size = "minute", number * 60
        else:
            unit, size = "day", number
    if unit == "minute" and not 1 <= size <= 390:
        raise ValueError("自定义分钟周期必须在 1 至 390 分钟之间。")
    if unit == "day" and not 1 <= size <= 365:
        raise ValueError("自定义日周期必须在 1 至 365 日之间。")
    return {
        "code": value,
        "unit": unit,
        "size": size,
        "intraday": unit == "minute",
    }


def refresh_alpaca_capability(symbol: str) -> dict:
    normalized = symbol.strip().upper()
    result = fetch_asset(normalized)
    settings = repository.set_alpaca_capability(
        normalized,
        supported=bool(result["supported"]),
        alpaca_symbol=result.get("symbol") or normalized,
        error=result.get("reason"),
    )
    return {**result, "symbol_settings": settings}


def _regular_session_parts(row: dict):
    utc_dt = datetime.fromtimestamp(
        int(row["minute_utc"]) * 60,
        tz=timezone.utc,
    )
    local_dt = utc_dt.astimezone(NEW_YORK)
    minute_of_day = local_dt.hour * 60 + local_dt.minute
    if not REGULAR_OPEN_MINUTE <= minute_of_day < REGULAR_CLOSE_MINUTE:
        return None
    return local_dt, minute_of_day


def _merge_group(rows: list[dict], label: str, end_label: str | None = None) -> dict:
    first, last = rows[0], rows[-1]
    result = {
        "date": label,
        "open": first["open"],
        "high": max(row["high"] for row in rows),
        "low": min(row["low"] for row in rows),
        "close": last["close"],
        "volume": sum(int(row.get("volume") or 0) for row in rows),
    }
    if end_label and end_label != label:
        result["endDate"] = end_label
    return result


def aggregate_intraday_rows(rows: Iterable[dict], interval_minutes: int) -> list[dict]:
    groups: dict[tuple[str, int], list[dict]] = {}
    labels: dict[tuple[str, int], str] = {}
    for row in rows:
        parts = _regular_session_parts(row)
        if not parts:
            continue
        local_dt, minute_of_day = parts
        bucket = (minute_of_day - REGULAR_OPEN_MINUTE) // interval_minutes
        key = (local_dt.date().isoformat(), bucket)
        groups.setdefault(key, []).append(row)
        bucket_minute = REGULAR_OPEN_MINUTE + bucket * interval_minutes
        labels[key] = (
            f"{local_dt.date().isoformat()} "
            f"{bucket_minute // 60:02d}:{bucket_minute % 60:02d}"
        )
    return [
        _merge_group(groups[key], labels[key])
        for key in sorted(groups)
    ]


def aggregate_daily_rows(rows: list[dict], spec: dict) -> list[dict]:
    if spec["unit"] == "day":
        size = int(spec["size"])
        if size == 1:
            return [dict(row) for row in rows]
        return [
            _merge_group(
                rows[index : index + size],
                rows[index]["date"],
                rows[min(index + size - 1, len(rows) - 1)]["date"],
            )
            for index in range(0, len(rows), size)
        ]

    groups: dict[str, list[dict]] = {}
    for row in rows:
        parsed = datetime.strptime(row["date"], "%Y-%m-%d").date()
        if spec["unit"] == "week":
            iso_year, iso_week, _ = parsed.isocalendar()
            key = f"{iso_year:04d}-W{iso_week:02d}"
        else:
            key = row["date"][:7]
        groups.setdefault(key, []).append(row)
    return [
        _merge_group(group, group[0]["date"], group[-1]["date"])
        for group in groups.values()
    ]


def get_chart_bars(symbol: str, period: str, limit: int = 1500) -> dict:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    spec = parse_bar_spec(period)
    requested_limit = max(8, min(int(limit), MAX_CHART_BARS))
    settings = repository.get_symbol(normalized)
    if spec["intraday"] and settings.get("alpaca_supported") is None:
        settings = refresh_alpaca_capability(normalized)["symbol_settings"]

    if not spec["intraday"]:
        rows = repository.get_daily_prices(normalized)
        bars = aggregate_daily_rows(rows, spec)[-requested_limit:]
        return {
            "ok": True,
            "symbol": normalized,
            "period": spec["code"],
            "intraday": False,
            "source": "database",
            "symbol_settings": settings,
            "data": bars,
            "warning": None,
        }

    if settings.get("alpaca_supported") is False:
        return {
            "ok": True,
            "symbol": normalized,
            "period": spec["code"],
            "intraday": True,
            "source": "database",
            "symbol_settings": settings,
            "data": [],
            "warning": settings.get("alpaca_error")
            or "Alpaca 不支持该标的，无法查看分钟级K线。",
        }

    before_minute = None
    raw_rows: list[dict] = []
    bars: list[dict] = []
    next_aggregate_at = max(
        20_000,
        int(requested_limit * min(int(spec["size"]), 195) * 1.5),
    )
    while True:
        page = intraday_repository.get_minute_bars(
            normalized,
            before_minute=before_minute,
            limit=20_000,
            descending=True,
        )
        if not page:
            break
        raw_rows.extend(page)
        before_minute = min(row["minute_utc"] for row in page)
        reached_end = len(page) < 20_000
        if len(raw_rows) >= next_aggregate_at or reached_end:
            bars = aggregate_intraday_rows(
                sorted(raw_rows, key=lambda row: row["minute_utc"]),
                int(spec["size"]),
            )
            if len(bars) >= requested_limit or reached_end:
                break
            next_aggregate_at = max(
                len(raw_rows) + 20_000,
                int(len(raw_rows) * 1.5),
            )

    sync_state = intraday_repository.get_sync_state(normalized)
    warning = None
    if not bars:
        warning = (
            "该标的尚未导入分钟数据，请点击更新按钮开始导入。"
            if settings.get("alpaca_supported") is not False
            else settings.get("alpaca_error")
        )
    elif sync_state.get("status") not in {"success"}:
        warning = "分钟历史仍在导入或上次导入未完成，当前展示已落库部分。"
    return {
        "ok": True,
        "symbol": normalized,
        "period": spec["code"],
        "intraday": True,
        "source": "alpaca/database",
        "symbol_settings": settings,
        "sync_state": sync_state,
        "data": bars[-requested_limit:],
        "warning": warning,
    }


def derive_daily_prices_from_minutes(
    symbol: str,
    *,
    start_at: str | None = None,
) -> dict:
    """Rebuild regular-session daily OHLCV while holding only one day in memory."""
    normalized = symbol.strip().upper()
    current_date = None
    current_rows: list[dict] = []
    daily_rows: list[dict] = []
    now_new_york = datetime.now(timezone.utc).astimezone(NEW_YORK)

    def flush() -> None:
        if not current_rows or current_date is None:
            return
        row = _merge_group(current_rows, current_date)
        row["source_provider"] = "alpaca"
        row["source_timeframe"] = "derived_1m"
        row["is_complete"] = (
            current_date < now_new_york.date().isoformat()
            or (
                current_date == now_new_york.date().isoformat()
                and now_new_york.time() >= time(16, 20)
            )
        )
        daily_rows.append(row)

    start_minute = (
        intraday_repository.iso_to_epoch_minute(start_at)
        if start_at
        else None
    )
    for row in intraday_repository.iter_minute_bars(
        normalized,
        start_minute=start_minute,
    ):
        parts = _regular_session_parts(row)
        if not parts:
            continue
        local_dt, _ = parts
        session_date = local_dt.date().isoformat()
        if current_date is not None and session_date != current_date:
            flush()
            current_rows = []
        current_date = session_date
        current_rows.append(row)
    flush()
    updated = repository.upsert_daily_prices(
        normalized,
        daily_rows,
        source_provider="alpaca",
        source_timeframe="derived_1m",
    )
    return {
        "symbol": normalized,
        "updated_rows": updated,
        "first_date": daily_rows[0]["date"] if daily_rows else None,
        "last_date": daily_rows[-1]["date"] if daily_rows else None,
    }
