from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import logging
import multiprocessing
import os
import platform
import shlex
import shutil
import subprocess
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template, request

from config import (
    ANALYSIS_MAX_WORKERS,
    APP_NAME,
    AUTO_OPEN_BROWSER,
    AUTO_SHUTDOWN_ON_BROWSER_CLOSE,
    BROWSER_OPEN_COMMAND,
    FLASK_HOST,
    FLASK_PORT,
    MAX_DB_PAGE_SIZE,
)
from database.db import backup_database, init_database
from database import repository
from services.api_errors import MarketDataError
from services.market_data_service import (
    get_market_data,
    sync_market_overview_daily_prices,
    update_full_market_data,
)
from services.analysis_overview_service import (
    build_trendline_overview_summary,
    merge_analysis_overview,
    snapshot_matches_signature,
)
from services.trendline_analysis_service import (
    ANALYSIS_CACHE_VERSION,
    analyze_symbol_trendlines,
    get_trendline_analysis_signature,
)


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
session_lock = threading.Lock()
last_heartbeat_at: datetime | None = None
shutdown_pending = False
overview_sync_lock = threading.Lock()
overview_sync_state = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "updated_at": None,
}
analysis_overview_lock = threading.Lock()
analysis_executor_lock = threading.Lock()
analysis_process_executor: ProcessPoolExecutor | None = None
analysis_overview_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current_symbol": None,
    "parallel_workers": 0,
    "remaining": 0,
    "last_result": None,
    "last_error": None,
    "updated_at": None,
    "rerun_requested": False,
}


class HeartbeatAccessLogFilter(logging.Filter):
    """Keep high-frequency session heartbeats out of the terminal."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/session/heartbeat" not in record.getMessage()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def mark_heartbeat() -> None:
    global last_heartbeat_at, shutdown_pending
    with session_lock:
        last_heartbeat_at = now_utc()
        shutdown_pending = False


@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.route("/api/market-data")
def market_data_query():
    return market_data_response(request.args.get("symbol", ""))


@app.route("/api/market-data/<path:symbol>")
def market_data(symbol: str):
    return market_data_response(symbol)


@app.route("/api/analysis/trendlines")
def trendline_analysis_query():
    return trendline_analysis_response(request.args.get("symbol", ""))


@app.route("/api/analysis/trendlines/<path:symbol>")
def trendline_analysis(symbol: str):
    return trendline_analysis_response(symbol)


def trendline_analysis_response(symbol: str):
    try:
        period = request.args.get("period", "1D")
        limit = int(request.args.get("limit", "150"))
        payload = analyze_symbol_trendlines(
            symbol,
            period=period,
            limit=limit,
            show_weekend_data=request.args.get("show_weekend_data"),
        )
        if period.upper() == "1D" and limit == 150:
            save_analysis_overview_snapshot(symbol, payload)
        return jsonify(payload)
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": str(exc),
                        "detail": None,
                    },
                }
            ),
            400,
        )
    except MarketDataError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 502
    except Exception as exc:
        app.logger.exception("Unexpected trendline analysis error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统识别趋势线时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


def market_data_response(symbol: str):
    try:
        return jsonify(get_market_data(symbol))
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": str(exc),
                        "detail": None,
                    },
                }
            ),
            400,
        )
    except MarketDataError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 502
    except Exception as exc:
        app.logger.exception("Unexpected market data error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统处理行情数据时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/market-data/update", methods=["POST"])
def update_market_data():
    payload = request.get_json(silent=True) or {}
    symbol = payload.get("symbol") or request.args.get("symbol", "")
    try:
        return jsonify(update_full_market_data(symbol))
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": str(exc),
                        "detail": None,
                    },
                }
            ),
            400,
        )
    except MarketDataError as exc:
        repository.log_api_request(
            provider="market_data",
            status="error",
            symbol=symbol.strip().upper(),
            error_code=exc.code,
            message=exc.detail or exc.message,
        )
        return jsonify({"ok": False, "error": exc.to_dict()}), 502
    except Exception as exc:
        app.logger.exception("Unexpected market data update error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统更新行情数据时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/market-overview")
def market_overview():
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "100"))
        return jsonify({"ok": True, **repository.list_market_overview(page, page_size)})
    except Exception as exc:
        app.logger.exception("Unexpected market overview error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统读取行情总览时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/analysis-overview")
def analysis_overview():
    try:
        market = repository.list_market_overview()
        snapshots = repository.list_latest_trendline_analysis_snapshots(
            ANALYSIS_CACHE_VERSION,
        )
        return jsonify({
            "ok": True,
            **merge_analysis_overview(market, snapshots),
            "refresh": analysis_overview_snapshot(),
        })
    except Exception as exc:
        app.logger.exception("Unexpected analysis overview error")
        return (
            jsonify({
                "ok": False,
                "error": {
                    "code": "UNKNOWN_ERROR",
                    "message": "系统读取 K 线分析总览时发生未知错误。",
                    "detail": str(exc),
                },
            }),
            500,
        )


@app.route("/api/analysis-overview/refresh", methods=["POST"])
def refresh_analysis_overview():
    try:
        started = start_analysis_overview_refresh()
        return jsonify({
            "ok": True,
            "started": started,
            **analysis_overview_snapshot(),
        })
    except Exception as exc:
        app.logger.exception("Unable to start analysis overview refresh")
        return jsonify({
            "ok": False,
            "error": {
                "code": "ANALYSIS_REFRESH_FAILED",
                "message": "无法启动 K 线分析总览更新。",
                "detail": str(exc),
            },
        }), 500


@app.route("/api/analysis-overview/refresh-status")
def analysis_overview_refresh_status():
    return jsonify({"ok": True, **analysis_overview_snapshot()})


@app.route("/api/analysis-overview/snapshot")
def analysis_overview_symbol_snapshot():
    symbol = request.args.get("symbol", "")
    try:
        snapshot = repository.get_latest_trendline_analysis_snapshot(
            symbol,
            ANALYSIS_CACHE_VERSION,
        )
        return jsonify({
            "ok": True,
            "snapshot": snapshot,
        })
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "error": {
                "code": "INVALID_INPUT",
                "message": str(exc),
            },
        }), 400


@app.route("/api/market-overview/order", methods=["PATCH"])
def market_overview_order():
    payload = request.get_json(silent=True) or {}
    try:
        symbols = payload.get("symbols") or []
        if not isinstance(symbols, list):
            raise ValueError("symbols must be a list")
        return jsonify({"ok": True, **repository.update_symbol_display_order(symbols)})
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ORDER",
                        "message": "行情总览排序请求无效。",
                        "detail": str(exc),
                    },
                }
            ),
            400,
        )
    except Exception as exc:
        app.logger.exception("Unexpected market overview order error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统保存行情总览排序时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/market-overview/sync-daily", methods=["POST"])
def market_overview_sync_daily():
    try:
        started = start_overview_sync()
        return jsonify({"ok": True, "running": True, "started": started, **overview_sync_snapshot()})
    except Exception as exc:
        app.logger.exception("Unexpected market overview daily sync error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统自动补齐行情总览日 K 时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/market-overview/sync-status")
def market_overview_sync_status():
    return jsonify({"ok": True, **overview_sync_snapshot()})


@app.route("/api/market-overview/refresh-prices", methods=["POST"])
def market_overview_refresh_prices():
    try:
        started = start_overview_sync()
        overview = repository.list_market_overview()
        items = [
            {
                "symbol": item["symbol"],
                "display_symbol": item["display_symbol"],
                "source": "database",
                "status": "success",
                "latest_price": item["latest_price"],
                "daily_change": item["daily_change"],
                "daily_change_percent": item["daily_change_percent"],
                "error": None,
            }
            for item in overview["items"]
        ]
        return jsonify(
            {
                "ok": True,
                "source": "database",
                "running": True,
                "started": started,
                "items": items,
                "updated_rows": 0,
            }
        )
    except Exception as exc:
        app.logger.exception("Unexpected market overview price refresh error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统刷新行情总览实时价格时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


def start_overview_sync() -> bool:
    with overview_sync_lock:
        if overview_sync_state["running"]:
            return False
        overview_sync_state["running"] = True
        overview_sync_state["last_error"] = None
        overview_sync_state["updated_at"] = now_utc().isoformat()

    threading.Thread(target=run_overview_sync, daemon=True).start()
    return True


def run_overview_sync() -> None:
    analysis_refresh_needed = False
    try:
        result = sync_market_overview_daily_prices()
        analysis_refresh_needed = True
        with overview_sync_lock:
            overview_sync_state["last_result"] = result
            overview_sync_state["last_error"] = None
            overview_sync_state["updated_at"] = now_utc().isoformat()
    except Exception as exc:
        app.logger.exception("Background market overview sync failed")
        with overview_sync_lock:
            overview_sync_state["last_error"] = str(exc)
            overview_sync_state["updated_at"] = now_utc().isoformat()
    finally:
        with overview_sync_lock:
            overview_sync_state["running"] = False
        if analysis_refresh_needed:
            start_analysis_overview_refresh(queue_if_running=True)


def overview_sync_snapshot() -> dict:
    with overview_sync_lock:
        return {
            "running": bool(overview_sync_state["running"]),
            "last_result": overview_sync_state["last_result"],
            "last_error": overview_sync_state["last_error"],
            "updated_at": overview_sync_state["updated_at"],
        }


def save_analysis_overview_snapshot(symbol: str, payload: dict) -> dict:
    summary = build_trendline_overview_summary(payload)
    canonical_symbol = (
        payload.get("canonical_symbol")
        or payload.get("symbol")
        or symbol
    )
    return repository.save_trendline_analysis_snapshot(
        canonical_symbol,
        payload,
        summary,
        ANALYSIS_CACHE_VERSION,
    )


def start_analysis_overview_refresh(queue_if_running: bool = False) -> bool:
    total = len(repository.list_overview_symbols())
    with analysis_overview_lock:
        if analysis_overview_state["running"]:
            if queue_if_running:
                analysis_overview_state["rerun_requested"] = True
            return False
        analysis_overview_state.update({
            "running": True,
            "total": total,
            "completed": 0,
            "current_symbol": None,
            "parallel_workers": 0,
            "remaining": total,
            "last_error": None,
            "updated_at": now_utc().isoformat(),
            "rerun_requested": False,
        })
    threading.Thread(
        target=run_analysis_overview_refresh,
        daemon=True,
    ).start()
    return True


def analysis_worker_count(
        task_count: int,
        available_cpus: int | None = None,
) -> int:
    """Keep one logical CPU available while bounding analysis concurrency."""
    if task_count <= 0:
        return 0
    if available_cpus is None:
        try:
            available_cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            available_cpus = os.cpu_count() or 1
    cpu_budget = max(1, int(available_cpus) - 1)
    return min(task_count, ANALYSIS_MAX_WORKERS, cpu_budget)


def _analysis_result(
        symbol: str,
        status: str,
        active_count: int | None = None,
        error: str | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "active_count": active_count,
        "error": error,
    }


def _mark_analysis_item_completed(remaining: int) -> None:
    with analysis_overview_lock:
        analysis_overview_state["completed"] += 1
        analysis_overview_state["remaining"] = max(0, remaining)
        analysis_overview_state["updated_at"] = now_utc().isoformat()


def run_analysis_overview_refresh() -> None:
    ordered_symbols: list[str] = []
    results_by_symbol: dict[str, dict] = {}
    pending_symbols: list[str] = []
    try:
        symbols = repository.list_overview_symbols()
        ordered_symbols = [item["common_symbol"] for item in symbols]
        with analysis_overview_lock:
            analysis_overview_state["total"] = len(symbols)
            analysis_overview_state["remaining"] = len(symbols)

        for item in symbols:
            symbol = item["common_symbol"]
            with analysis_overview_lock:
                analysis_overview_state["current_symbol"] = symbol
                analysis_overview_state["updated_at"] = now_utc().isoformat()
            try:
                signature = get_trendline_analysis_signature(
                    symbol,
                    period="1D",
                    limit=150,
                )
                snapshot = repository.get_latest_trendline_analysis_snapshot(
                    signature["canonical_symbol"],
                    ANALYSIS_CACHE_VERSION,
                )
                cache_hit = snapshot_matches_signature(
                    snapshot,
                    signature,
                )
                if cache_hit:
                    saved = save_analysis_overview_snapshot(
                        symbol,
                        snapshot["payload"],
                    )
                    results_by_symbol[symbol] = _analysis_result(
                        symbol,
                        "cached",
                        saved["active_count"],
                    )
                    _mark_analysis_item_completed(
                        len(symbols) - len(results_by_symbol)
                    )
                else:
                    pending_symbols.append(symbol)
            except Exception as exc:
                app.logger.exception(
                    "Analysis overview failed for %s",
                    symbol,
                )
                results_by_symbol[symbol] = _analysis_result(
                    symbol,
                    "error",
                    error=str(exc),
                )
                _mark_analysis_item_completed(
                    len(symbols) - len(results_by_symbol)
                )

        worker_count = analysis_worker_count(len(pending_symbols))
        with analysis_overview_lock:
            analysis_overview_state["current_symbol"] = None
            analysis_overview_state["parallel_workers"] = worker_count
            analysis_overview_state["remaining"] = len(pending_symbols)
            analysis_overview_state["updated_at"] = now_utc().isoformat()

        if worker_count == 1:
            for symbol in pending_symbols:
                try:
                    payload = analyze_symbol_trendlines(
                        symbol,
                        period="1D",
                        limit=150,
                    )
                    saved = save_analysis_overview_snapshot(symbol, payload)
                    results_by_symbol[symbol] = _analysis_result(
                        symbol,
                        "success",
                        saved["active_count"],
                    )
                except Exception as exc:
                    app.logger.exception(
                        "Analysis overview failed for %s",
                        symbol,
                    )
                    results_by_symbol[symbol] = _analysis_result(
                        symbol,
                        "error",
                        error=str(exc),
                    )
                finally:
                    remaining = sum(
                        item not in results_by_symbol
                        for item in pending_symbols
                    )
                    _mark_analysis_item_completed(remaining)
        elif worker_count > 1:
            # Spawn avoids inheriting Flask thread locks or SQLite connections.
            # Workers only calculate; the parent serializes snapshot writes.
            spawn_context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(
                    max_workers=worker_count,
                    mp_context=spawn_context,
            ) as executor:
                set_analysis_process_executor(executor)
                try:
                    futures = {
                        executor.submit(
                            analyze_symbol_trendlines,
                            symbol,
                            "1D",
                            150,
                        ): symbol
                        for symbol in pending_symbols
                    }
                    for future in as_completed(futures):
                        symbol = futures[future]
                        try:
                            payload = future.result()
                            saved = save_analysis_overview_snapshot(
                                symbol,
                                payload,
                            )
                            results_by_symbol[symbol] = _analysis_result(
                                symbol,
                                "success",
                                saved["active_count"],
                            )
                        except Exception as exc:
                            app.logger.exception(
                                "Analysis overview failed for %s",
                                symbol,
                            )
                            results_by_symbol[symbol] = _analysis_result(
                                symbol,
                                "error",
                                error=str(exc),
                            )
                        finally:
                            remaining = sum(
                                item not in results_by_symbol
                                for item in pending_symbols
                            )
                            _mark_analysis_item_completed(remaining)
                finally:
                    clear_analysis_process_executor(executor)

        results = [
            results_by_symbol[symbol]
            for symbol in ordered_symbols
            if symbol in results_by_symbol
        ]
        failed = sum(1 for item in results if item["status"] == "error")
        with analysis_overview_lock:
            analysis_overview_state["last_result"] = {
                "items": results,
                "total": len(results),
                "failed": failed,
            }
            analysis_overview_state["last_error"] = None
    except Exception as exc:
        app.logger.exception("Background analysis overview refresh failed")
        with analysis_overview_lock:
            analysis_overview_state["last_error"] = str(exc)
    finally:
        rerun_requested = False
        with analysis_overview_lock:
            rerun_requested = bool(
                analysis_overview_state["rerun_requested"]
            )
            analysis_overview_state["running"] = False
            analysis_overview_state["current_symbol"] = None
            analysis_overview_state["parallel_workers"] = 0
            analysis_overview_state["remaining"] = 0
            analysis_overview_state["updated_at"] = now_utc().isoformat()
        if rerun_requested:
            start_analysis_overview_refresh()


def analysis_overview_snapshot() -> dict:
    with analysis_overview_lock:
        return {
            "running": bool(analysis_overview_state["running"]),
            "total": int(analysis_overview_state["total"]),
            "completed": int(analysis_overview_state["completed"]),
            "current_symbol": analysis_overview_state["current_symbol"],
            "parallel_workers": int(
                analysis_overview_state["parallel_workers"]
            ),
            "remaining": int(analysis_overview_state["remaining"]),
            "last_result": analysis_overview_state["last_result"],
            "last_error": analysis_overview_state["last_error"],
            "updated_at": analysis_overview_state["updated_at"],
        }


@app.route("/api/market-overview/<path:symbol>", methods=["PATCH"])
def patch_market_overview_symbol(symbol: str):
    payload = request.get_json(silent=True) or {}
    try:
        show_in_overview = payload.get("show_in_overview")
        if show_in_overview is None:
            raise ValueError("show_in_overview is required")
        settings = repository.set_symbol_overview_visibility(symbol, bool(show_in_overview))
        return jsonify({"ok": True, "symbol_settings": settings, **repository.list_market_overview()})
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_SYMBOL",
                        "message": "行情总览标的请求无效。",
                        "detail": str(exc),
                    },
                }
            ),
            400,
        )
    except Exception as exc:
        app.logger.exception("Unexpected market overview symbol patch error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统更新行情总览标的时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/db/tables")
def db_tables():
    return jsonify({"ok": True, "tables": repository.list_tables()})


@app.route("/api/db/table/<table_name>")
def db_table(table_name: str):
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", str(MAX_DB_PAGE_SIZE)))
        search = request.args.get("search", "")
        return jsonify({"ok": True, **repository.get_table_page(table_name, page, page_size, search)})
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_TABLE_REQUEST",
                        "message": "数据库表请求无效。",
                        "detail": str(exc),
                    },
                }
            ),
            400,
        )


@app.route("/api/db/backup", methods=["POST"])
def db_backup():
    try:
        backup_path = backup_database()
        return jsonify(
            {
                "ok": True,
                "message": "数据库备份完成。",
                "path": str(backup_path),
                "filename": backup_path.name,
            }
        )
    except Exception as exc:
        app.logger.exception("Database backup failed")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "BACKUP_FAILED",
                        "message": "数据库备份失败。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/indicators")
def indicators():
    favorite_param = request.args.get("favorite")
    favorite = None
    if favorite_param is not None:
        favorite = favorite_param in {"1", "true", "True", "yes"}
    return jsonify({"ok": True, "indicators": repository.list_indicators(favorite)})


@app.route("/api/indicators", methods=["POST"])
def create_indicator():
    payload = request.get_json(silent=True) or {}
    try:
        indicator = repository.get_or_create_indicator(
            payload.get("indicator_type", ""),
            payload.get("params") or {},
            payload.get("name"),
        )
        return jsonify({"ok": True, "indicator": indicator})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_INDICATOR", "message": str(exc)}}), 400


@app.route("/api/indicators/<int:indicator_id>", methods=["PATCH"])
def patch_indicator(indicator_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "indicator": repository.update_indicator(indicator_id, payload)})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_INDICATOR", "message": str(exc)}}), 400


@app.route("/api/symbols/<path:symbol>/chart-views")
def symbol_chart_views(symbol: str):
    normalized = symbol.strip().upper()
    return jsonify({"ok": True, "views": repository.ensure_symbol_chart_views(normalized)})


@app.route("/api/symbols/<path:symbol>/settings", methods=["PATCH"])
def patch_symbol_settings(symbol: str):
    payload = request.get_json(silent=True) or {}
    try:
        normalized = symbol.strip().upper()
        settings = repository.update_symbol_settings(normalized, payload)
        return jsonify({"ok": True, "symbol_settings": settings})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_SYMBOL_SETTINGS", "message": str(exc)}}), 400


@app.route("/api/symbols/<path:symbol>/chart-views/<view_code>/indicators")
def symbol_view_indicators(symbol: str, view_code: str):
    try:
        normalized = symbol.strip().upper()
        return jsonify(
            {
                "ok": True,
                "indicators": repository.list_symbol_indicators(normalized, view_code),
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_VIEW", "message": str(exc)}}), 400


@app.route("/api/symbols/<path:symbol>/chart-views/<view_code>/indicators", methods=["POST"])
def add_symbol_view_indicator(symbol: str, view_code: str):
    payload = request.get_json(silent=True) or {}
    try:
        normalized = symbol.strip().upper()
        result = repository.add_symbol_indicator(
            normalized,
            view_code,
            indicator_id=payload.get("indicator_id"),
            indicator_type=payload.get("indicator_type"),
            params=payload.get("params"),
            name=payload.get("name"),
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_INDICATOR", "message": str(exc)}}), 400


@app.route(
    "/api/symbols/<path:symbol>/chart-views/<view_code>/indicators/<int:symbol_indicator_id>",
    methods=["PATCH"],
)
def patch_symbol_view_indicator(symbol: str, view_code: str, symbol_indicator_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        normalized = symbol.strip().upper()
        result = repository.update_symbol_indicator(
            normalized,
            view_code,
            symbol_indicator_id,
            payload,
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_INDICATOR", "message": str(exc)}}), 400


@app.route(
    "/api/symbols/<path:symbol>/chart-views/<view_code>/indicators/<int:symbol_indicator_id>",
    methods=["DELETE"],
)
def delete_symbol_view_indicator(symbol: str, view_code: str, symbol_indicator_id: int):
    try:
        normalized = symbol.strip().upper()
        result = repository.delete_symbol_indicator(normalized, view_code, symbol_indicator_id)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "INVALID_INDICATOR", "message": str(exc)}}), 400


@app.route("/api/session/heartbeat", methods=["POST"])
def heartbeat():
    mark_heartbeat()
    return jsonify({"ok": True})


@app.route("/api/session/close", methods=["POST"])
def close_session():
    if AUTO_SHUTDOWN_ON_BROWSER_CLOSE:
        schedule_shutdown_check()
    return jsonify({"ok": True})


@app.route("/api/system/shutdown", methods=["POST"])
def shutdown_system():
    threading.Thread(target=shutdown_process, daemon=True).start()
    return jsonify({"ok": True, "message": "系统正在退出。"})


def schedule_shutdown_check() -> None:
    global shutdown_pending
    with session_lock:
        shutdown_pending = True

    thread = threading.Thread(target=shutdown_if_inactive, daemon=True)
    thread.start()


def shutdown_if_inactive() -> None:
    time.sleep(4)
    with session_lock:
        should_shutdown = (
            shutdown_pending
            and last_heartbeat_at is not None
            and now_utc() - last_heartbeat_at > timedelta(seconds=3)
        )
    if should_shutdown:
        os._exit(0)


def shutdown_process() -> None:
    # Let Flask flush the shutdown response, then release spawned analysis
    # workers so the parent terminal is not left waiting on child processes.
    time.sleep(0.25)
    terminate_child_processes()
    time.sleep(0.05)
    os._exit(0)


def terminate_child_processes() -> None:
    terminate_analysis_process_executor()
    children = multiprocessing.active_children()
    for process in children:
        if process.is_alive():
            process.terminate()
    for process in children:
        process.join(timeout=0.5)


def set_analysis_process_executor(executor: ProcessPoolExecutor) -> None:
    global analysis_process_executor
    with analysis_executor_lock:
        analysis_process_executor = executor


def clear_analysis_process_executor(executor: ProcessPoolExecutor) -> None:
    global analysis_process_executor
    with analysis_executor_lock:
        if analysis_process_executor is executor:
            analysis_process_executor = None


def terminate_analysis_process_executor() -> None:
    global analysis_process_executor
    with analysis_executor_lock:
        executor = analysis_process_executor
        analysis_process_executor = None
    if executor is None:
        return
    processes = list(
        (getattr(executor, "_processes", None) or {}).values()
    )
    for process in processes:
        if process.is_alive():
            process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)


def configure_access_logging() -> None:
    logging.getLogger("werkzeug").addFilter(HeartbeatAccessLogFilter())


def open_browser() -> None:
    url = f"http://{FLASK_HOST}:{FLASK_PORT}"
    time.sleep(0.6)
    if open_browser_with_command(url):
        return
    webbrowser.open_new_tab(url)


def open_browser_with_command(url: str) -> bool:
    if BROWSER_OPEN_COMMAND:
        return run_browser_command(shlex.split(BROWSER_OPEN_COMMAND) + [url])

    if is_wsl() and shutil.which("cmd.exe"):
        return run_browser_command(["cmd.exe", "/c", "start", "", url])

    if platform.system() == "Darwin" and shutil.which("open"):
        return run_browser_command(["open", url])

    if platform.system() == "Linux" and shutil.which("xdg-open"):
        return run_browser_command(["xdg-open", url])

    return False


def run_browser_command(command: list[str]) -> bool:
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def is_wsl() -> bool:
    release = platform.release().lower()
    return "microsoft" in release or "wsl" in release


def main() -> None:
    init_database()
    configure_access_logging()
    start_analysis_overview_refresh()
    if AUTO_OPEN_BROWSER:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
