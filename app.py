from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from services.market_data_service import get_market_data


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
session_lock = threading.Lock()
last_heartbeat_at: datetime | None = None
shutdown_pending = False


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


@app.route("/api/market-data/<symbol>")
def market_data(symbol: str):
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


@app.route("/api/db/tables")
def db_tables():
    return jsonify({"ok": True, "tables": repository.list_tables()})


@app.route("/api/db/table/<table_name>")
def db_table(table_name: str):
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", str(MAX_DB_PAGE_SIZE)))
        return jsonify({"ok": True, **repository.get_table_page(table_name, page, page_size)})
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


@app.route("/api/symbols/<symbol>/chart-views")
def symbol_chart_views(symbol: str):
    normalized = symbol.strip().upper()
    return jsonify({"ok": True, "views": repository.ensure_symbol_chart_views(normalized)})


@app.route("/api/symbols/<symbol>/chart-views/<view_code>/indicators")
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


@app.route("/api/symbols/<symbol>/chart-views/<view_code>/indicators", methods=["POST"])
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
    "/api/symbols/<symbol>/chart-views/<view_code>/indicators/<int:symbol_indicator_id>",
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
    "/api/symbols/<symbol>/chart-views/<view_code>/indicators/<int:symbol_indicator_id>",
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
    time.sleep(0.4)
    os._exit(0)


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
    if AUTO_OPEN_BROWSER:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
