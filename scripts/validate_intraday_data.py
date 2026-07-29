from __future__ import annotations

import argparse
import json

from database import repository
from services.intraday_bar_service import derive_daily_prices_from_minutes
from services.intraday_import_service import import_symbols_history
from services.intraday_quality_service import (
    VALIDATION_SYMBOLS,
    validate_intraday_storage,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import and validate Alpaca minute history from 2020."
    )
    parser.add_argument(
        "--import-history",
        action="store_true",
        help="Download missing/resumable history before validation.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=VALIDATION_SYMBOLS,
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Pause each symbol after this many pages; useful for a smoke test.",
    )
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    if args.import_history:
        result = import_symbols_history(
            args.symbols,
            max_pages=args.max_pages,
            max_workers=args.workers,
            progress=lambda item: print(
                json.dumps(
                    {
                        "symbol": item["symbol"],
                        "pages": item["job"]["pages_fetched"],
                        "rows": item["job"]["rows_written"],
                        "page_rows": item["page_rows"],
                        "remaining": item["rate_limit"]["remaining"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            ),
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        derived = {}
        for symbol, item in result["results"].items():
            if not item.get("complete"):
                continue
            repository.set_alpaca_capability(
                symbol,
                supported=True,
                alpaca_symbol=symbol,
            )
            derived[symbol] = derive_daily_prices_from_minutes(symbol)
        print(
            json.dumps(
                {"derived_daily": derived},
                ensure_ascii=False,
            ),
            flush=True,
        )

    print(
        json.dumps(
            validate_intraday_storage(args.symbols),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
