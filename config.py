from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "market_data.sqlite"
SCHEMA_PATH = BASE_DIR / "database" / "schema.sql"

load_dotenv(BASE_DIR / ".env")

APP_NAME = "交易分析决策系统"
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
AUTO_OPEN_BROWSER = os.getenv("AUTO_OPEN_BROWSER", "true").lower() == "true"
AUTO_SHUTDOWN_ON_BROWSER_CLOSE = (
    os.getenv("AUTO_SHUTDOWN_ON_BROWSER_CLOSE", "true").lower() == "true"
)
BROWSER_OPEN_COMMAND = os.getenv("BROWSER_OPEN_COMMAND", "")
ANALYSIS_MAX_WORKERS = max(
    1,
    min(4, int(os.getenv("ANALYSIS_MAX_WORKERS", "4"))),
)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_BASE_URL = "https://api.twelvedata.com/time_series"
MARKET_INTERVAL = "1day"
LOOKBACK_DAYS = 365
API_OUTPUT_SIZE = 500
FULL_HISTORY_START_DATE = "2020-01-01"
FULL_HISTORY_OUTPUT_SIZE = 5000
REQUEST_TIMEOUT_SECONDS = 20

MAX_DB_PAGE_SIZE = 50
