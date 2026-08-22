from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "market_data.sqlite"
INTRADAY_DATABASE_PATH = DATA_DIR / "intraday_data.sqlite"
SCHEMA_PATH = BASE_DIR / "database" / "schema.sql"
INTRADAY_SCHEMA_PATH = BASE_DIR / "database" / "intraday_schema.sql"

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
REALTIME_MAX_WORKERS = max(
    1,
    min(8, int(os.getenv("REALTIME_MAX_WORKERS", "4"))),
)
REALTIME_EVENT_GRACE_SECONDS = max(
    10,
    int(os.getenv("REALTIME_EVENT_GRACE_SECONDS", "60")),
)
REALTIME_RECOVERY_STALE_SECONDS = max(
    60,
    int(os.getenv("REALTIME_RECOVERY_STALE_SECONDS", "300")),
)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_BASE_URL = "https://api.twelvedata.com/time_series"
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets/v2"
ALPACA_TRADING_BASE_URL = os.getenv(
    "ALPACA_TRADING_BASE_URL",
    "https://paper-api.alpaca.markets/v2",
)
# Alpaca Basic currently allows 200 Market Data REST requests/minute. Keep a
# larger owner-wide safety margin for background imports and interactive calls.
ALPACA_SAFE_REQUESTS_PER_MINUTE = 150
INTRADAY_PRICE_SCALE = 1_000_000
MARKET_INTERVAL = "1day"
LOOKBACK_DAYS = 365
API_OUTPUT_SIZE = 500
FULL_HISTORY_START_DATE = "2020-01-01"
FULL_HISTORY_OUTPUT_SIZE = 5000
REQUEST_TIMEOUT_SECONDS = 20

# Provider-level minute history limits are distinct from daily-history limits.
# They are used by importers and strict intraday backtests alike.
KNOWN_MINUTE_HISTORY_STARTS = {
    "MAGS": {"date": "2023-04-11", "source": "alpaca"},
    "BTC/USD": {"date": "2021-01-01", "source": "alpaca_crypto"},
}

MAX_DB_PAGE_SIZE = 50
