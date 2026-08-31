from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import logging
import multiprocessing
import os
import platform
import shlex
import shutil
import subprocess
import threading
import time
from uuid import uuid4
import webbrowser

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

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
from database.db import backup_databases, init_database
from database.intraday_db import init_intraday_database
from database import repository
from database import backtest_repository
from services.alpaca_data_client import fetch_stock_bars
from services.api_errors import MarketDataError
from services.market_data_service import (
    get_market_data,
    sync_market_overview_daily_prices,
    update_full_market_data,
)
from services.intraday_bar_service import get_chart_bars
from services.indicator_service import (
    attach_overview_indicator_values,
    build_indicator_series,
)
from services.corporate_action_adjustment_service import (
    stored_adjusted_daily_payload,
)
from services.analysis_overview_service import (
    build_key_zone_overview_summary,
    build_trendline_overview_summary,
    merge_analysis_overview,
    snapshot_matches_signature,
)
from services.trendline_analysis_service import (
    ANALYSIS_CACHE_VERSION,
    analyze_symbol_trendlines,
    get_trendline_analysis_signature,
)
from services.key_zone_analysis_service import (
    KEY_ZONE_ALGORITHM_VERSION,
    analyze_symbol_key_zones,
)
from services.backtest.errors import BacktestError
from services.backtest.service import (
    code_strategy_catalog,
    create_default_strategy,
    create_strategy as create_backtest_strategy_service,
    duplicate_strategy,
    repair_saved_strategy_data,
    run_manager as backtest_run_manager,
    update_strategy as update_backtest_strategy_service,
    validate_saved_strategy,
)
from services.backtest.presets import ensure_shipped_strategy_presets
from services.backtest.export import build_run_xls
from services.backtest.analysis import (
    build_analysis,
    build_analysis_meta,
    build_candles as build_backtest_analysis_candles,
    build_decision as build_backtest_analysis_decision,
    purge_analysis_cache,
)
from services.backtest.validation import validate_strategy_payload
from database import realtime_repository
from services.realtime_mail import bootstrap_env_qq_channel
from services.realtime_mail import (
    encrypt_secret,
    normalize_recipients,
    send_smtp,
    validate_message_template,
)
from services.realtime_scheduler import run_manager as realtime_run_manager
from services.market_overview_coordinator import market_overview_coordinator
from services.realtime_dashboard_service import (
    build_realtime_dashboard,
    clear_realtime_dashboard_cache,
    dashboard_recommendations,
)
from services.realtime_panel_script import (
    generate_panel_settings,
    validate_panel_script,
    validate_panel_settings,
)
from services.realtime_presets import ensure_shipped_realtime_tasks
from services.market_context import annotate_us_market_sessions


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
session_lock = threading.Lock()
last_heartbeat_at: datetime | None = None
shutdown_pending = False
market_data_update_lock = threading.Lock()
market_data_update_jobs: dict[str, dict] = {}
analysis_overview_lock = threading.Lock()
analysis_executor_lock = threading.Lock()
analysis_process_executor: ProcessPoolExecutor | None = None
analysis_overview_state = {
    "running": False,
    "analysis_type": None,
    "phase": "idle",
    "total": 0,
    "checked": 0,
    "cache_hits": 0,
    "pending": 0,
    "completed": 0,
    "current_symbol": None,
    "parallel_workers": 0,
    "remaining": 0,
    "last_result": None,
    "last_error": None,
    "updated_at": None,
    "rerun_requested": None,
}


class HeartbeatAccessLogFilter(logging.Filter):
    """Keep high-frequency browser status polling out of the terminal."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return (
            "/api/session/heartbeat" not in message
            and '"GET /api/realtime/tasks' not in message
        )


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
    return market_data_response(
        request.args.get("symbol", ""),
        include_intraday=request.args.get("include_intraday") in {"1", "true", "yes"},
        adjustment=request.args.get("adjustment"),
    )


@app.route("/api/market-data/<path:symbol>")
def market_data(symbol: str):
    return market_data_response(symbol)


@app.route("/api/market-bars")
def market_bars_query():
    try:
        return jsonify(
            get_chart_bars(
                request.args.get("symbol", ""),
                request.args.get("period", "1D"),
                int(request.args.get("limit", "1500")),
                request.args.get("adjustment", "all"),
            )
        )
    except ValueError as exc:
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": str(exc),
                    "detail": None,
                },
            }
        ), 400
    except MarketDataError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 502
    except Exception as exc:
        app.logger.exception("Unexpected aggregated market bars error")
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_ERROR",
                    "message": "系统读取K线数据时发生未知错误。",
                    "detail": str(exc),
                },
            }
        ), 500


@app.route("/api/alpaca/stock-bars")
def alpaca_stock_bars():
    """Diagnostic-only Alpaca endpoint; it does not write to SQLite."""
    try:
        return jsonify(
            fetch_stock_bars(
                request.args.get("symbol", ""),
                timeframe=request.args.get("timeframe", "1Min"),
                start=request.args.get("start", "2020-01-01"),
                end=request.args.get("end") or None,
                feed=request.args.get("feed", "sip"),
                limit=int(request.args.get("limit", "1000")),
                max_pages=int(request.args.get("max_pages", "1")),
            )
        )
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
        app.logger.exception("Unexpected Alpaca market data error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统测试 Alpaca 行情数据时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


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
            show_weekend_data=(
                request.args.get("show_non_us_market_days")
                if request.args.get("show_non_us_market_days") is not None
                else request.args.get("show_weekend_data")
            ),
            adjustment=request.args.get("adjustment", "all"),
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


def market_data_response(
    symbol: str,
    include_intraday: bool = False,
    adjustment: str | None = None,
):
    try:
        kwargs = {"include_intraday": include_intraday}
        if adjustment is not None:
            kwargs["adjustment"] = adjustment
        payload = get_market_data(symbol, **kwargs)
        if payload.get("data"):
            payload["data"] = annotate_us_market_sessions(payload["data"])
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
    include_intraday = str(
        payload.get("include_intraday", request.args.get("include_intraday", ""))
    ).strip().lower() in {"1", "true", "yes", "on"}
    background = str(payload.get("background", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    query_only = str(payload.get("query_only", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    if background:
        try:
            job, started = start_market_data_update(
                symbol,
                include_intraday=include_intraday,
                query_only=query_only,
            )
            return jsonify(
                {
                    "ok": True,
                    "running": True,
                    "started": started,
                    "job": job,
                }
            ), 202
        except ValueError as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": str(exc),
                        "detail": None,
                    },
                }
            ), 400
    try:
        return jsonify(
            update_full_market_data(
                symbol,
                initialize_intraday=include_intraday,
            )
        )
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


@app.route("/api/market-data/update-status/<job_id>")
def market_data_update_status(job_id: str):
    with market_data_update_lock:
        job = market_data_update_jobs.get(job_id)
        if not job:
            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UPDATE_JOB_NOT_FOUND",
                        "message": "行情更新任务不存在或已过期。",
                        "detail": None,
                    },
                }
            ), 404
        return jsonify({"ok": True, "job": dict(job)})


def start_market_data_update(
    symbol: str,
    *,
    include_intraday: bool,
    query_only: bool = False,
) -> tuple[dict, bool]:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    with market_data_update_lock:
        for job in market_data_update_jobs.values():
            if job["running"] and job["symbol"] == normalized:
                # All callers attach to the same per-symbol task. A request for
                # minutes upgrades a queued/running daily-only task; the worker
                # checks this flag before publishing its final result.
                if include_intraday and not job["include_intraday"]:
                    job["include_intraday"] = True
                    job["message"] = "已合并分钟数据请求，当前任务完成后继续导入"
                    job["updated_at"] = now_utc().isoformat()
                return dict(job), False
        job_id = uuid4().hex
        now = now_utc().isoformat()
        job = {
            "id": job_id,
            "symbol": normalized,
            "include_intraday": bool(include_intraday),
            "query_only": bool(query_only),
            "running": True,
            "stage": "queued",
            "progress": 0.0,
            "current_date": None,
            "pages": 0,
            "rows": 0,
            "message": "行情更新任务已开始",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        market_data_update_jobs[job_id] = job
        if len(market_data_update_jobs) > 20:
            completed_ids = [
                key
                for key, item in market_data_update_jobs.items()
                if not item["running"] and key != job_id
            ]
            for key in completed_ids[: len(market_data_update_jobs) - 20]:
                market_data_update_jobs.pop(key, None)

    threading.Thread(
        target=run_market_data_update,
        args=(job_id, normalized),
        daemon=True,
    ).start()
    return dict(job), True


def run_market_data_update(
    job_id: str,
    symbol: str,
) -> None:
    def progress(payload: dict) -> None:
        with market_data_update_lock:
            job = market_data_update_jobs.get(job_id)
            if not job or not job["running"]:
                return
            for field in (
                "stage", "progress", "current_date", "pages", "rows", "message"
            ):
                if field in payload:
                    job[field] = payload[field]
            job["updated_at"] = now_utc().isoformat()

    try:
        while True:
            with market_data_update_lock:
                job = market_data_update_jobs[job_id]
                include_intraday = bool(job["include_intraday"])
                query_only = bool(job.get("query_only"))
            if query_only:
                result = get_market_data(
                    symbol,
                    include_intraday=include_intraday,
                    progress_callback=progress,
                )
            else:
                result = update_full_market_data(
                    symbol,
                    initialize_intraday=include_intraday,
                    progress_callback=progress,
                )

            # Check and publish under one lock so a late duplicate request
            # cannot miss the daily-to-minute upgrade window.
            with market_data_update_lock:
                job = market_data_update_jobs[job_id]
                if bool(job["include_intraday"]) and not include_intraday:
                    job["message"] = "日线数据已完成，继续导入分钟数据"
                    job["updated_at"] = now_utc().isoformat()
                    continue
                job.update(
                    {
                        "running": False,
                        "stage": "completed",
                        "progress": 1.0,
                        "current_date": (
                            result.get("data", [{}])[-1].get("date")
                            if result.get("data")
                            else job.get("current_date")
                        ),
                        "message": "行情数据更新完成",
                        "result": result,
                        "error": None,
                        "updated_at": now_utc().isoformat(),
                    }
                )
                break
    except Exception as exc:
        app.logger.exception("Background market data update failed")
        if isinstance(exc, MarketDataError):
            error = exc.to_dict()
        elif isinstance(exc, ValueError):
            error = {
                "code": "INVALID_INPUT",
                "message": str(exc),
                "detail": None,
            }
        else:
            error = {
                "code": "UNKNOWN_ERROR",
                "message": "系统更新行情数据时发生未知错误。",
                "detail": str(exc),
            }
        with market_data_update_lock:
            job = market_data_update_jobs[job_id]
            job.update(
                {
                    "running": False,
                    "stage": "failed",
                    "message": error["message"],
                    "error": error,
                    "updated_at": now_utc().isoformat(),
                }
            )


@app.route("/api/analysis/key-zones")
def key_zone_analysis_query():
    return key_zone_analysis_response(request.args.get("symbol", ""))


@app.route("/api/analysis/key-zones/<path:symbol>")
def key_zone_analysis(symbol: str):
    return key_zone_analysis_response(symbol)


def key_zone_analysis_response(symbol: str):
    """Run and persist on-demand key zones outside the overview snapshots."""
    try:
        period = request.args.get("period", "1D")
        limit = int(request.args.get("limit", "150"))
        payload = analyze_symbol_key_zones(
            symbol,
            period=period,
            limit=limit,
            show_weekend_data=(
                request.args.get("show_non_us_market_days")
                if request.args.get("show_non_us_market_days") is not None
                else request.args.get("show_weekend_data")
            ),
            adjustment=request.args.get("adjustment", "all"),
        )
        canonical_symbol = (
            payload.get("canonical_symbol")
            or payload.get("symbol")
            or symbol
        )
        repository.save_key_zone_analysis_snapshot(
            canonical_symbol,
            payload,
            KEY_ZONE_ALGORITHM_VERSION,
        )
        return jsonify(payload)
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "error": {
                "code": "INVALID_INPUT",
                "message": str(exc),
                "detail": None,
            },
        }), 400
    except MarketDataError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 502
    except Exception as exc:
        app.logger.exception("Unexpected key-zone analysis error")
        return jsonify({
            "ok": False,
            "error": {
                "code": "UNKNOWN_ERROR",
                "message": "系统识别水平支撑/压力区时发生未知错误。",
                "detail": str(exc),
            },
        }), 500


@app.route("/api/analysis/key-zone-snapshot")
def key_zone_analysis_snapshot():
    try:
        show_value = (
            request.args.get("show_non_us_market_days")
            if request.args.get("show_non_us_market_days") is not None
            else request.args.get("show_weekend_data")
        )
        show_weekend_data = str(show_value or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        snapshot = repository.get_latest_key_zone_analysis_snapshot(
            request.args.get("symbol", ""),
            KEY_ZONE_ALGORITHM_VERSION,
            period=request.args.get("period", "1D"),
            window_size=int(request.args.get("limit", "150")),
            show_weekend_data=show_weekend_data,
            adjustment=request.args.get("adjustment", "all"),
        )
        return jsonify({"ok": True, "snapshot": snapshot})
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "error": {
                "code": "INVALID_INPUT",
                "message": str(exc),
            },
        }), 400


@app.route("/api/market-overview")
def market_overview():
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "100"))
        raw_indicator_ids = request.args.get("indicator_ids", "")
        requested_ids: list[int] = []
        for raw_value in raw_indicator_ids.split(","):
            if not raw_value.strip():
                continue
            indicator_id = int(raw_value)
            if indicator_id not in requested_ids:
                requested_ids.append(indicator_id)
        if len(requested_ids) > 3:
            raise ValueError("行情总览最多显示 3 个自定义指标。")

        favorite_by_id = {
            int(indicator["id"]): indicator
            for indicator in repository.list_indicators(favorite=True)
        }
        selected_indicators = [
            favorite_by_id[indicator_id]
            for indicator_id in requested_ids
            if indicator_id in favorite_by_id
        ]
        overview = repository.list_market_overview(page, page_size)
        overview["indicator_standard_price_basis"] = "all_adjusted"
        overview["indicator_action_source"] = "stored_only"
        if selected_indicators:
            daily_rows_by_symbol: dict[str, list[dict]] = {}
            indicator_metadata: dict[str, dict] = {}
            for item in overview["items"]:
                symbol = item["symbol"]
                rows = repository.get_daily_prices(symbol, include_metadata=True)
                settings = repository.get_symbol(symbol)
                adjusted = stored_adjusted_daily_payload(
                    symbol,
                    rows,
                    settings,
                    mode="all",
                )
                indicator_rows = annotate_us_market_sessions(adjusted["rows"])
                if not settings.get("show_non_us_market_days", True):
                    indicator_rows = [
                        row for row in indicator_rows
                        if row.get("is_us_market_session")
                    ]
                daily_rows_by_symbol[symbol] = indicator_rows
                item["indicator_adjustment_warning"] = adjusted.get("warning")
                indicator_metadata[symbol] = {
                    "price_basis": (
                        "all_adjusted"
                        if adjusted["adjustment"] == "all"
                        else adjusted["adjustment"]
                    ),
                    "as_of": item.get("latest_price_updated_at"),
                    "action_source": adjusted.get("action_source"),
                }
            attach_overview_indicator_values(
                overview,
                selected_indicators,
                daily_rows_by_symbol,
                indicator_metadata,
            )
        else:
            overview["selected_indicators"] = []
            for item in overview["items"]:
                item["indicator_values"] = {}
        return jsonify({"ok": True, **overview})
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "error": {"code": "INVALID_INDICATOR", "message": str(exc)},
        }), 400
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
        key_zone_snapshots = repository.list_latest_key_zone_analysis_snapshots(
            KEY_ZONE_ALGORITHM_VERSION,
        )
        return jsonify({
            "ok": True,
            **merge_analysis_overview(market, snapshots, key_zone_snapshots),
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
        payload = request.get_json(silent=True) or {}
        analysis_type = str(payload.get("analysis_type") or "trendline")
        started = start_analysis_overview_refresh(analysis_type)
        return jsonify({
            "ok": True,
            "started": started,
            **analysis_overview_snapshot(),
        })
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "error": {
                "code": "INVALID_ANALYSIS_TYPE",
                "message": str(exc),
            },
        }), 400
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
        started = start_overview_sync(reason="manual")
        return jsonify({"ok": True, "running": True, "started": started, **overview_sync_snapshot()})
    except Exception as exc:
        app.logger.exception("Unexpected market overview daily sync error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "系统自动更新行情总览时发生未知错误。",
                        "detail": str(exc),
                    },
                }
            ),
            500,
        )


@app.route("/api/market-overview/sync-status")
def market_overview_sync_status():
    return jsonify({"ok": True, **overview_sync_snapshot()})


@app.route("/api/market-overview/auto-refresh", methods=["PATCH"])
def market_overview_auto_refresh():
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload or not isinstance(payload["enabled"], bool):
        return jsonify({
            "ok": False,
            "error": {"code": "INVALID_INPUT", "message": "enabled 必须是布尔值。"},
        }), 400
    try:
        return jsonify({
            "ok": True,
            **market_overview_coordinator.set_auto_enabled(bool(payload["enabled"])),
        })
    except Exception as exc:
        app.logger.exception("Unable to update market overview auto refresh")
        return jsonify({
            "ok": False,
            "error": {"code": "AUTO_REFRESH_FAILED", "message": str(exc)},
        }), 500


@app.route("/api/market-overview/refresh-prices", methods=["POST"])
def market_overview_refresh_prices():
    try:
        started = start_overview_sync(reason="manual")
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


def start_overview_sync(*, reason: str = "manual") -> bool:
    return market_overview_coordinator.trigger(reason=reason)


def run_overview_sync() -> None:
    # Compatibility entry point used by maintenance/tests; the application
    # itself schedules refreshes only through the coordinator.
    sync_market_overview_daily_prices(reason="maintenance")


def overview_sync_snapshot() -> dict:
    return market_overview_coordinator.snapshot()


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


def analyze_symbol_overview_item(
        symbol: str,
        analysis_type: str,
        period: str = "1D",
        limit: int = 150,
) -> dict:
    if analysis_type == "trendline":
        return analyze_symbol_trendlines(symbol, period, limit)
    if analysis_type == "key_zone":
        return analyze_symbol_key_zones(symbol, period, limit)
    raise ValueError("Unsupported overview analysis type")


def save_analysis_overview_item(
        symbol: str,
        analysis_type: str,
        payload: dict,
) -> dict:
    if analysis_type == "trendline":
        return save_analysis_overview_snapshot(symbol, payload)
    canonical_symbol = (
        payload.get("canonical_symbol")
        or payload.get("symbol")
        or symbol
    )
    repository.save_key_zone_analysis_snapshot(
        canonical_symbol,
        payload,
        KEY_ZONE_ALGORITHM_VERSION,
    )
    summary = build_key_zone_overview_summary(payload)
    return {**summary, "active_count": summary["critical_count"]}


def start_analysis_overview_refresh(
        analysis_type: str = "trendline",
        queue_if_running: bool = False,
) -> bool:
    if analysis_type not in {"trendline", "key_zone"}:
        raise ValueError("Unsupported overview analysis type")
    total = len(repository.list_overview_symbols())
    with analysis_overview_lock:
        if analysis_overview_state["running"]:
            if queue_if_running:
                analysis_overview_state["rerun_requested"] = analysis_type
            return False
        analysis_overview_state.update({
            "running": True,
            "analysis_type": analysis_type,
            "phase": "checking_cache",
            "total": total,
            "checked": 0,
            "cache_hits": 0,
            "pending": 0,
            "completed": 0,
            "current_symbol": None,
            "parallel_workers": 0,
            "remaining": total,
            "last_error": None,
            "updated_at": now_utc().isoformat(),
            "rerun_requested": None,
        })
    threading.Thread(
        target=run_analysis_overview_refresh,
        args=(analysis_type,),
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


def _mark_analysis_item_checked(pending: int) -> None:
    with analysis_overview_lock:
        analysis_overview_state["checked"] += 1
        analysis_overview_state["pending"] = max(0, pending)
        analysis_overview_state["updated_at"] = now_utc().isoformat()


def run_analysis_overview_refresh(analysis_type: str = "trendline") -> None:
    ordered_symbols: list[str] = []
    results_by_symbol: dict[str, dict] = {}
    pending_symbols: list[str] = []
    try:
        symbols = repository.list_overview_symbols()
        ordered_symbols = [item["common_symbol"] for item in symbols]
        with analysis_overview_lock:
            analysis_overview_state["phase"] = "checking_cache"
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
                if analysis_type == "trendline":
                    snapshot = (
                        repository.get_latest_trendline_analysis_snapshot(
                            signature["canonical_symbol"],
                            ANALYSIS_CACHE_VERSION,
                        )
                    )
                else:
                    snapshot = repository.get_latest_key_zone_analysis_snapshot(
                        signature["canonical_symbol"],
                        KEY_ZONE_ALGORITHM_VERSION,
                        period="1D",
                        window_size=150,
                        show_weekend_data=bool(
                            signature.get("show_weekend_data")
                        ),
                        adjustment="all",
                    )
                if snapshot_matches_signature(snapshot, signature):
                    summary = (
                        snapshot["summary"]
                        if analysis_type == "trendline"
                        else build_key_zone_overview_summary(
                            snapshot["payload"]
                        )
                    )
                    results_by_symbol[symbol] = _analysis_result(
                        symbol,
                        "cached",
                        int(
                            summary.get("active_count")
                            or summary.get("critical_count")
                            or 0
                        ),
                    )
                    with analysis_overview_lock:
                        analysis_overview_state["cache_hits"] += 1
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
            finally:
                _mark_analysis_item_checked(len(pending_symbols))

        worker_count = analysis_worker_count(len(pending_symbols))
        with analysis_overview_lock:
            analysis_overview_state["phase"] = "calculating"
            analysis_overview_state["current_symbol"] = None
            analysis_overview_state["parallel_workers"] = worker_count
            analysis_overview_state["remaining"] = len(pending_symbols)
            analysis_overview_state["updated_at"] = now_utc().isoformat()

        if worker_count == 1:
            for symbol in pending_symbols:
                try:
                    payload = analyze_symbol_overview_item(
                        symbol,
                        analysis_type,
                        period="1D",
                        limit=150,
                    )
                    saved = save_analysis_overview_item(
                        symbol,
                        analysis_type,
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
                            analyze_symbol_overview_item,
                            symbol,
                            analysis_type,
                            "1D",
                            150,
                        ): symbol
                        for symbol in pending_symbols
                    }
                    for future in as_completed(futures):
                        symbol = futures[future]
                        try:
                            payload = future.result()
                            saved = save_analysis_overview_item(
                                symbol,
                                analysis_type,
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
                "cache_hits": sum(
                    1 for item in results if item["status"] == "cached"
                ),
                "calculated": sum(
                    1 for item in results if item["status"] == "success"
                ),
            }
            analysis_overview_state["last_error"] = None
    except Exception as exc:
        app.logger.exception("Background analysis overview refresh failed")
        with analysis_overview_lock:
            analysis_overview_state["last_error"] = str(exc)
    finally:
        rerun_requested = None
        with analysis_overview_lock:
            rerun_requested = analysis_overview_state["rerun_requested"]
            analysis_overview_state["running"] = False
            analysis_overview_state["phase"] = (
                "error"
                if analysis_overview_state["last_error"]
                else "complete"
            )
            analysis_overview_state["current_symbol"] = None
            analysis_overview_state["parallel_workers"] = 0
            analysis_overview_state["remaining"] = 0
            analysis_overview_state["updated_at"] = now_utc().isoformat()
        if rerun_requested:
            start_analysis_overview_refresh(rerun_requested)


def analysis_overview_snapshot() -> dict:
    with analysis_overview_lock:
        return {
            "running": bool(analysis_overview_state["running"]),
            "analysis_type": analysis_overview_state["analysis_type"],
            "phase": analysis_overview_state["phase"],
            "total": int(analysis_overview_state["total"]),
            "checked": int(analysis_overview_state["checked"]),
            "cache_hits": int(analysis_overview_state["cache_hits"]),
            "pending": int(analysis_overview_state["pending"]),
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


def backtest_error_response(exc: Exception):
    if isinstance(exc, BacktestError):
        error = exc.to_dict()
        status = 400
    elif isinstance(exc, RuntimeError):
        error = {
            "code": "REVISION_CONFLICT",
            "message": str(exc),
            "detail": None,
        }
        status = 409
    elif isinstance(exc, ValueError):
        error = {
            "code": "INVALID_BACKTEST_REQUEST",
            "message": str(exc),
            "detail": None,
        }
        status = 400
    else:
        app.logger.exception("Unexpected backtest API error")
        error = {
            "code": "UNKNOWN_BACKTEST_ERROR",
            "message": "历史回测服务发生未知错误。",
            "detail": str(exc),
        }
        status = 500
    return jsonify({"ok": False, "error": error}), status


@app.route("/api/backtest/strategies")
def backtest_strategies():
    try:
        ensure_shipped_strategy_presets()
        strategies = backtest_repository.list_strategies()
        latest_runs = backtest_repository.latest_runs_by_strategy()
        return jsonify(
            {
                "ok": True,
                "strategies": [
                    {
                        **strategy,
                        "latest_run": latest_runs.get(strategy["id"]),
                    }
                    for strategy in strategies
                ],
            }
        )
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/strategies", methods=["POST"])
def create_backtest_strategy():
    payload = request.get_json(silent=True) or {}
    try:
        if str(payload.get("design_mode") or "visual") != "visual":
            raise ValueError("代码策略只能由项目代码内置，不能在前端或公共接口中新建。")
        if "definition" not in payload:
            strategy = create_default_strategy(
                name=str(payload.get("name") or "未命名策略"),
                design_mode="visual",
                selection_mode=str(payload.get("selection_mode") or "single"),
            )
        else:
            strategy = create_backtest_strategy_service(payload)
        return jsonify({"ok": True, "strategy": strategy}), 201
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/strategies/<int:strategy_id>")
def get_backtest_strategy(strategy_id: int):
    try:
        return jsonify(
            {
                "ok": True,
                "strategy": backtest_repository.get_strategy(strategy_id),
                "runs": backtest_repository.list_runs(strategy_id, limit=20),
            }
        )
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/strategies/<int:strategy_id>", methods=["PATCH"])
def patch_backtest_strategy(strategy_id: int):
    try:
        strategy = update_backtest_strategy_service(
            strategy_id,
            request.get_json(silent=True) or {},
        )
        return jsonify({"ok": True, "strategy": strategy})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/strategies/<int:strategy_id>", methods=["DELETE"])
def delete_backtest_strategy(strategy_id: int):
    try:
        strategy = backtest_repository.delete_strategy(strategy_id)
        return jsonify({"ok": True, "strategy": strategy})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route(
    "/api/backtest/strategies/<int:strategy_id>/duplicate",
    methods=["POST"],
)
def duplicate_backtest_strategy(strategy_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        strategy = duplicate_strategy(strategy_id, payload.get("name"))
        return jsonify({"ok": True, "strategy": strategy}), 201
    except Exception as exc:
        return backtest_error_response(exc)


@app.route(
    "/api/backtest/strategies/<int:strategy_id>/validate",
    methods=["POST"],
)
def validate_backtest_strategy(strategy_id: int):
    try:
        return jsonify(validate_saved_strategy(strategy_id))
    except Exception as exc:
        return backtest_error_response(exc)


@app.route(
    "/api/backtest/strategies/<int:strategy_id>/repair-recent-data",
    methods=["POST"],
)
def repair_backtest_strategy_data(strategy_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(repair_saved_strategy_data(strategy_id, payload.get("symbol", "")))
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/code-strategies")
def backtest_code_strategies():
    try:
        return jsonify({"ok": True, "strategies": code_strategy_catalog()})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route(
    "/api/backtest/strategies/<int:strategy_id>/runs",
    methods=["POST"],
)
def start_backtest_run(strategy_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        run = backtest_run_manager.start(strategy_id, payload.get("settings"))
        return jsonify({"ok": True, "run": run}), 202
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/strategies/<int:strategy_id>/runs")
def list_backtest_runs(strategy_id: int):
    try:
        return jsonify(
            {
                "ok": True,
                "runs": backtest_repository.list_runs(strategy_id),
            }
        )
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>")
def get_backtest_run(run_id: int):
    try:
        return jsonify({"ok": True, "run": backtest_run_manager.run_status(run_id)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs")
def list_all_backtest_runs():
    try:
        strategy_id = request.args.get("strategy_id")
        return jsonify({
            "ok": True,
            **backtest_repository.list_runs_overview(
                page=int(request.args.get("page", "1")),
                page_size=int(request.args.get("page_size", "25")),
                strategy_id=int(strategy_id) if strategy_id else None,
                status=request.args.get("status") or None,
            ),
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/detail")
def get_backtest_run_detail(run_id: int):
    try:
        return jsonify({
            "ok": True,
            "run": backtest_repository.get_run_detail(run_id),
            "equity_points": backtest_repository.get_equity_points(run_id),
            "trades": backtest_repository.get_trades(run_id),
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/analysis/meta")
def get_backtest_analysis_meta(run_id: int):
    try:
        snapshot = backtest_run_manager.analysis_snapshot(run_id)
        return jsonify({"ok": True, **build_analysis_meta(snapshot)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/analysis")
def get_backtest_analysis(run_id: int):
    try:
        snapshot = backtest_run_manager.analysis_snapshot(run_id)
        return jsonify({
            "ok": True,
            **build_analysis(
                snapshot,
                request.args.get("start_date", ""),
                request.args.get("end_date", ""),
            ),
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/analysis/decision")
def get_backtest_analysis_decision(run_id: int):
    try:
        trading_date = request.args.get("date", "")
        snapshot = backtest_run_manager.analysis_snapshot(
            run_id,
            trading_date=trading_date,
        )
        return jsonify({"ok": True, **build_backtest_analysis_decision(snapshot, trading_date)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/analysis/candles")
def get_backtest_analysis_candles(run_id: int):
    try:
        snapshot = backtest_run_manager.analysis_snapshot(run_id)
        return jsonify({
            "ok": True,
            **build_backtest_analysis_candles(
                snapshot,
                request.args.get("symbol", ""),
                request.args.get("start_date", ""),
                request.args.get("end_date", ""),
            ),
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/deletions", methods=["POST"])
def delete_backtest_runs():
    payload = request.get_json(silent=True) or {}
    try:
        if payload.get("confirm") is not True:
            raise ValueError("必须明确确认不可逆删除。")
        result = backtest_repository.delete_runs(payload.get("run_ids") or [])
        backtest_run_manager.purge_deleted_runs(result["run_ids"])
        purge_analysis_cache(result["run_ids"])
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/cancel", methods=["POST"])
def cancel_backtest_run(run_id: int):
    try:
        return jsonify({"ok": True, "run": backtest_run_manager.cancel(run_id)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/results")
def backtest_run_results(run_id: int):
    try:
        return jsonify({"ok": True, **backtest_run_manager.result(run_id)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/logs")
def backtest_run_logs(run_id: int):
    try:
        backtest_repository.get_run(run_id, include_snapshot=False)
        logs = backtest_repository.get_logs(
            run_id,
            level=request.args.get("level", "DEBUG"),
            after_sequence=int(request.args.get("after", "0")),
            limit=int(request.args.get("limit", "1000")),
        )
        return jsonify({"ok": True, "logs": logs})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/logs.xls")
def backtest_run_logs_xls(run_id: int):
    try:
        payload = build_run_xls(run_id)
        return send_file(
            BytesIO(payload),
            mimetype="application/vnd.ms-excel",
            as_attachment=True,
            download_name=f"backtest-{run_id}.xls",
            max_age=0,
        )
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/backtest/runs/<int:run_id>/events")
def backtest_run_events(run_id: int):
    try:
        equity_after = max(0, int(request.args.get("equity_after", "0")))
        trade_after = max(0, int(request.args.get("trade_after", "0")))
        log_after = max(0, int(request.args.get("log_after", "0")))
        backtest_repository.get_run(run_id, include_snapshot=False)
    except Exception as exc:
        return backtest_error_response(exc)

    @stream_with_context
    def stream():
        cursors = {
            "equity": equity_after,
            "trade": trade_after,
            "log": log_after,
        }
        while True:
            payload = backtest_run_manager.events_since(
                run_id,
                equity_after=cursors["equity"],
                trade_after=cursors["trade"],
                log_after=cursors["log"],
            )
            cursors = payload["next"]
            yield (
                "event: update\n"
                f"data: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n"
            )
            if payload["run"]["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.75)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _realtime_task_payload(payload: dict, *, current: dict | None = None) -> tuple[dict, dict, dict]:
    current = current or {}
    strategy_id = payload.get("strategy_id", current.get("strategy_id"))
    if strategy_id is None:
        raise ValueError("必须选择一个历史回测策略。")
    strategy = backtest_repository.get_strategy(int(strategy_id))
    strategy_snapshot = payload.get("strategy_snapshot", current.get("strategy_snapshot", strategy))
    validate_strategy_payload(strategy_snapshot)
    settings = {**strategy.get("default_settings", {}), **(current.get("settings") or {}), **(payload.get("settings") or {})}
    notification = {**(current.get("notification_settings") or {}), **(payload.get("notification_settings") or {})}
    notification.setdefault("enabled", False)
    validate_message_template(notification.get("subject_template") or "")
    validate_message_template(notification.get("body_template") or "")
    if notification.get("enabled"):
        if not notification.get("channel_id"):
            raise ValueError("启用邮件通知时必须选择邮件通道。")
        normalize_recipients(notification.get("recipients"))
        if strategy.get("design_mode") != "code" and not str(notification.get("body_template") or "").strip():
            raise ValueError("非代码策略启用邮件时必须填写信息内容模板。")
    portfolio = {**(current.get("portfolio_state") or {}), **(payload.get("portfolio_state") or {})}
    portfolio.setdefault("cash", float(settings.get("initial_capital", 100000)))
    portfolio.setdefault("positions", {})
    return strategy, settings, {"notification": notification, "portfolio": portfolio}


@app.route("/api/realtime/tasks")
def realtime_tasks():
    try:
        tasks = realtime_repository.list_tasks()
        for task in tasks:
            runs = realtime_repository.list_runs(task["id"], limit=1)
            task["latest_run"] = runs[0] if runs else None
            if task.get("strategy_snapshot", {}).get("selection_mode") == "competition":
                try:
                    dashboard = build_realtime_dashboard(task["id"])
                    task["overview_recommendations"] = dashboard_recommendations(
                        dashboard, limit=3
                    )
                    task["overview_recommendations_calculated_at"] = dashboard.get(
                        "calculated_at"
                    )
                except Exception as exc:
                    task["overview_recommendations"] = []
                    task["overview_recommendations_error"] = str(exc)
        return jsonify({"ok": True, "tasks": tasks})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks", methods=["POST"])
def create_realtime_task():
    payload = request.get_json(silent=True) or {}
    try:
        strategy, settings, extras = _realtime_task_payload(payload)
        name = str(payload.get("name") or "").strip()
        if not name:
            existing = {item["name"] for item in realtime_repository.list_tasks()}
            index = 1
            base = strategy["name"]
            name = f"{base} #{index}"
            while name in existing:
                index += 1
                name = f"{base} #{index}"
        task = realtime_repository.create_task(
            name=name,
            strategy=strategy,
            follow_strategy=bool(payload.get("follow_strategy", True)),
            settings=settings,
            notification_settings=extras["notification"],
            portfolio_state=extras["portfolio"],
            panel_settings=generate_panel_settings(strategy),
        )
        return jsonify({"ok": True, "task": task}), 201
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>")
def get_realtime_task(task_id: int):
    try:
        task = realtime_run_manager.status(task_id)
        task["runs"] = realtime_repository.list_runs(task_id, limit=20)
        if str(request.args.get("include_logs") or "").lower() in {"1", "true", "yes"}:
            task["events"] = realtime_repository.list_events(task_id, limit=50)
            task["notifications"] = realtime_repository.list_notifications(task_id, limit=50)
        return jsonify({"ok": True, "task": task})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>", methods=["PATCH"])
def patch_realtime_task(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        current = realtime_repository.get_task(task_id)
        strategy, settings, extras = _realtime_task_payload(payload, current=current)
        follow = bool(payload.get("follow_strategy", current["follow_strategy"]))
        snapshot = current["strategy_snapshot"]
        if not follow and "strategy_snapshot" in payload:
            snapshot = payload["strategy_snapshot"]
        elif follow and current["follow_strategy"]:
            snapshot = current["strategy_snapshot"]
        elif follow:
            snapshot = strategy
        task = realtime_repository.update_task(
            task_id,
            name=str(payload["name"]).strip() if "name" in payload else None,
            strategy_snapshot=snapshot,
            source_strategy_revision=int(snapshot["revision"]),
            source_code_version=snapshot.get("code_version"),
            follow_strategy=follow,
            settings=settings,
            notification_settings=extras["notification"],
            portfolio_state=extras["portfolio"],
            expected_revision=payload.get("revision"),
        )
        if (
            snapshot.get("design_mode") == "visual"
            and not (current.get("panel_settings") or {}).get("customized")
            and (
                not (current.get("panel_settings") or {}).get("script")
                or snapshot.get("definition")
                != current["strategy_snapshot"].get("definition")
                or int(snapshot.get("revision") or 0)
                != int(current["strategy_snapshot"].get("revision") or 0)
            )
        ):
            task = realtime_repository.update_panel_settings(
                task_id, generate_panel_settings(snapshot)
            )
        return jsonify({"ok": True, "task": task})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>", methods=["DELETE"])
def delete_realtime_task(task_id: int):
    try:
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            raise ValueError("必须明确确认删除实时决策任务。")
        return jsonify({"ok": True, "task": realtime_repository.soft_delete_task(task_id)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/start", methods=["POST"])
def start_realtime_task(task_id: int):
    try:
        return jsonify({"ok": True, "task": realtime_run_manager.start(task_id)}), 202
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/stop", methods=["POST"])
def stop_realtime_task(task_id: int):
    try:
        return jsonify({"ok": True, "task": realtime_run_manager.stop(task_id)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/validate", methods=["POST"])
def validate_realtime_task(task_id: int):
    try:
        task = realtime_repository.get_task(task_id)
        strategy = validate_strategy_payload(task["strategy_snapshot"])
        return jsonify({
            "ok": True,
            "events": _events_for_realtime_strategy(strategy),
            "strategy_revision": task["source_strategy_revision"],
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/dashboard")
def realtime_task_dashboard(task_id: int):
    try:
        force = str(request.args.get("force") or "").lower() in {"1", "true", "yes"}
        return jsonify({
            "ok": True,
            "dashboard": build_realtime_dashboard(task_id, force=force),
            "overview_refresh": overview_sync_snapshot(),
        })
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/panel", methods=["PATCH"])
def patch_realtime_task_panel(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        task = realtime_repository.get_task(task_id)
        if task["strategy_snapshot"].get("design_mode") != "visual":
            raise ValueError("代码策略的实时面板必须在对应策略代码中修改。")
        panel_settings = {
            **(task.get("panel_settings") or {}),
            "script": payload.get("script", (task.get("panel_settings") or {}).get("script")),
            "customized": True,
        }
        panel_settings = validate_panel_settings(panel_settings, task["strategy_snapshot"])
        updated = realtime_repository.update_panel_settings(
            task_id,
            panel_settings,
            expected_panel_revision=payload.get("panel_revision"),
        )
        clear_realtime_dashboard_cache(task_id)
        return jsonify({"ok": True, "task": updated})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/panel/validate", methods=["POST"])
def validate_realtime_task_panel(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        task = realtime_repository.get_task(task_id)
        if task["strategy_snapshot"].get("design_mode") != "visual":
            raise ValueError("代码策略使用内置面板定义，无需编辑面板脚本。")
        parsed = validate_panel_script(str(payload.get("script") or ""))
        return jsonify({"ok": True, "parsed": parsed})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/panel/regenerate", methods=["POST"])
def regenerate_realtime_task_panel(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        task = realtime_repository.get_task(task_id)
        if task["strategy_snapshot"].get("design_mode") != "visual":
            raise ValueError("代码策略使用内置面板定义，不能生成 JSON 面板脚本。")
        generated = generate_panel_settings(task["strategy_snapshot"])
        updated = realtime_repository.update_panel_settings(
            task_id,
            generated,
            expected_panel_revision=payload.get("panel_revision"),
        )
        clear_realtime_dashboard_cache(task_id)
        return jsonify({"ok": True, "task": updated})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/tasks/<int:task_id>/logs")
def realtime_task_logs(task_id: int):
    try:
        realtime_repository.get_task(task_id)
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        legacy_before = request.args.get("before_id")
        event_before = request.args.get("before_event_id") or legacy_before
        notification_before = request.args.get("before_notification_id") or legacy_before
        event_before_id = int(event_before) if event_before else None
        notification_before_id = int(notification_before) if notification_before else None
        kind = str(request.args.get("kind") or "all").lower()
        if kind not in {"all", "decision", "mail"}:
            raise ValueError("日志类型必须为 all、decision 或 mail。")
        events = (
            realtime_repository.list_events(task_id, limit=limit, before_id=event_before_id)
            if kind in {"all", "decision"} else []
        )
        notifications = (
            realtime_repository.list_notifications(task_id, limit=limit, before_id=notification_before_id)
            if kind in {"all", "mail"} else []
        )
        return jsonify({"ok": True, "events": events, "notifications": notifications})
    except Exception as exc:
        return backtest_error_response(exc)


def _events_for_realtime_strategy(strategy: dict) -> list[str]:
    from services.realtime_scheduler import _events_for_strategy
    return _events_for_strategy(strategy)


@app.route("/api/realtime/tasks/<int:task_id>/events")
def realtime_task_events(task_id: int):
    try:
        realtime_repository.get_task(task_id)
        after = max(0, int(request.args.get("after", "0")))
    except Exception as exc:
        return backtest_error_response(exc)

    @stream_with_context
    def stream():
        cursor = after
        while True:
            task = realtime_run_manager.status(task_id)
            events = [item for item in realtime_repository.list_events(task_id, limit=500) if int(item["id"]) > cursor]
            if events:
                cursor = max(int(item["id"]) for item in events)
            yield "event: update\ndata: " + json.dumps({"task": task, "events": events}, ensure_ascii=False, allow_nan=False) + "\n\n"
            if task["runtime_state"] in {"stopped", "error"}:
                break
            time.sleep(1.0)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/realtime/email-channels")
def realtime_email_channels():
    try:
        with __import__("database.db", fromlist=["get_connection"]).get_connection() as conn:
            rows = conn.execute("SELECT id FROM email_channels ORDER BY id").fetchall()
        return jsonify({"ok": True, "channels": [realtime_repository.get_email_channel(row["id"]) for row in rows]})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/email-channels", methods=["POST"])
def create_realtime_email_channel():
    payload = request.get_json(silent=True) or {}
    try:
        secret = str(payload.pop("secret", "")).strip()
        if not secret:
            raise ValueError("必须填写邮箱授权码。")
        sender = str(payload.get("sender_email") or payload.get("username") or "").strip()
        if not sender:
            raise ValueError("必须填写发件邮箱。")
        if payload.get("provider") in {"gmail_smtp", "qq_smtp"}:
            defaults = {
                "gmail_smtp": {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "security_mode": "ssl"},
                "qq_smtp": {"smtp_host": "smtp.qq.com", "smtp_port": 465, "security_mode": "ssl"},
            }[payload["provider"]]
            for key, value in defaults.items():
                payload.setdefault(key, value)
        channel = realtime_repository.create_email_channel(payload, secret_ciphertext=encrypt_secret(secret))
        return jsonify({"ok": True, "channel": channel}), 201
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/email-channels/<int:channel_id>", methods=["PATCH"])
def patch_realtime_email_channel(channel_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        secret_value = payload.pop("secret", None)
        secret_ciphertext = encrypt_secret(str(secret_value).strip()) if secret_value else None
        return jsonify({"ok": True, "channel": realtime_repository.update_email_channel(channel_id, payload, secret_ciphertext=secret_ciphertext)})
    except Exception as exc:
        return backtest_error_response(exc)


@app.route("/api/realtime/email-channels/<int:channel_id>/test", methods=["POST"])
def test_realtime_email_channel(channel_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        channel = realtime_repository.get_email_channel(channel_id)
        recipient = str(payload.get("recipient") or channel["sender_email"] or "").strip()
        normalize_recipients([recipient])
        send_smtp(channel_id, recipient=recipient, subject="交易分析决策系统测试邮件", body="邮件通道测试成功。")
        realtime_repository.mark_email_channel_test(channel_id, ok=True)
        return jsonify({"ok": True, "message": "测试邮件已提交。"})
    except Exception as exc:
        realtime_repository.mark_email_channel_test(channel_id, ok=False, error=str(exc))
        return backtest_error_response(exc)


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
    payload = request.get_json(silent=True) or {}
    try:
        targets = payload.get("targets", ["main"])
        if not isinstance(targets, list):
            raise ValueError("targets must be a list")
        backups = backup_databases(targets)
        return jsonify(
            {
                "ok": True,
                "message": "数据库备份完成。",
                "backups": backups,
                "path": backups[0]["path"] if len(backups) == 1 else None,
                "filename": backups[0]["filename"] if len(backups) == 1 else None,
            }
        )
    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_BACKUP_TARGET",
                        "message": "数据库备份选择无效。",
                        "detail": str(exc),
                    },
                }
            ),
            400,
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
        indicators = repository.list_symbol_indicators(normalized, view_code)
        if request.args.get("with_values") not in {"1", "true", "yes"}:
            return jsonify({"ok": True, "indicators": indicators})

        adjustment = request.args.get("adjustment", "all")
        limit = int(request.args.get("limit", "2000"))
        bars_payload = get_chart_bars(
            normalized,
            view_code,
            limit,
            adjustment,
        )
        rows = list(bars_payload.get("data") or [])
        settings = bars_payload.get("symbol_settings") or {}
        # get_chart_bars applies the symbol's NYSE non-session setting before
        # aggregation, so indicator points and rendered bars share one series.
        latest = rows[-1] if rows else {}
        price_basis = (
            "all_adjusted"
            if bars_payload.get("adjustment") == "all"
            else str(bars_payload.get("adjustment") or "raw")
        )
        return jsonify(
            {
                "ok": True,
                "bars": rows,
                "indicators": build_indicator_series(
                    rows,
                    indicators,
                    price_basis=price_basis,
                    as_of=latest.get("updated_at") or latest.get("date"),
                ),
                "calculation": {
                    "price_basis": price_basis,
                    "as_of": latest.get("updated_at") or latest.get("date"),
                    "is_provisional": not bool(latest.get("is_complete", True)) if rows else False,
                    "source": bars_payload.get("source"),
                    "period": bars_payload.get("period") or view_code,
                },
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
    if should_shutdown and not backtest_run_manager.has_active_runs() and not realtime_run_manager.has_active_tasks():
        os._exit(0)


def shutdown_process() -> None:
    # Let Flask flush the shutdown response, then release spawned analysis
    # workers so the parent terminal is not left waiting on child processes.
    time.sleep(0.25)
    terminate_child_processes()
    time.sleep(0.05)
    os._exit(0)


def terminate_child_processes() -> None:
    market_overview_coordinator.stop()
    backtest_run_manager.shutdown()
    realtime_run_manager.shutdown()
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
    init_intraday_database()
    ensure_shipped_strategy_presets()
    ensure_shipped_realtime_tasks()
    bootstrap_env_qq_channel()
    backtest_run_manager.recover_interrupted_runs()
    realtime_run_manager.start_services()
    configure_access_logging()
    market_overview_coordinator.start()
    if AUTO_OPEN_BROWSER:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
