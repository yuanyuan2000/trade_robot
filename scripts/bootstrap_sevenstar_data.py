from __future__ import annotations

import argparse
import json

from database.db import init_database
from database.intraday_db import init_intraday_database
from services.backtest.presets import SEVENSTAR_LARGE, SEVENSTAR_SMALL
from services.intraday_bar_service import derive_daily_prices_from_minutes
from services.intraday_import_service import import_symbols_history
from services.intraday_quality_service import validate_intraday_storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resumably import and validate SevenStar minute history."
    )
    parser.add_argument("--pool", choices=("small", "large"), default="small")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Pause after N pages per symbol for a resumable smoke test.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Override the selected preset pool.",
    )
    args = parser.parse_args()

    init_database()
    init_intraday_database()
    symbols = args.symbols or (
        SEVENSTAR_SMALL if args.pool == "small" else SEVENSTAR_LARGE
    )
    result = import_symbols_history(
        symbols,
        start=args.start,
        end=args.end,
        max_pages=args.max_pages,
        max_workers=args.workers,
        progress=lambda item: print(
            json.dumps(
                {
                    "symbol": item["symbol"],
                    "pages": item["job"]["pages_fetched"],
                    "rows": item["job"]["rows_written"],
                    "page_rows": item["page_rows"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        ),
    )
    derived = {}
    for symbol, item in result["results"].items():
        if item.get("complete"):
            derived[symbol] = derive_daily_prices_from_minutes(symbol)
    output = {
        "import": result,
        "derived_daily": derived,
        "quality": validate_intraday_storage(symbols),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
