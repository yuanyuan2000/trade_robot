r"""Probe the local TdxQuant HTTP service without changing application data.

Run this script with the Windows Python interpreter because TdxW binds the
HTTP service to the Windows loopback interface::

    .venv\Scripts\python.exe scripts\probe_tdxquant.py

The default probe is read-only.  Pass ``--refresh`` explicitly to ask the
TdxQuant client to refresh a small sample of 1d/1m K-line cache first.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


DEFAULT_URL = "http://127.0.0.1:17709/"
DEFAULT_SYMBOLS = ("600519.SH", "000001.SZ")


class TdxQuantProbeError(RuntimeError):
    """Raised when the local HTTP service cannot satisfy a probe request."""


@dataclass(frozen=True)
class RpcResult:
    method: str
    elapsed_ms: float
    value: Any


class TdxQuantHttpClient:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self._request_id = 0
        self._session = requests.Session()

    def call(self, method: str, **params: Any) -> RpcResult:
        self._request_id += 1
        payload = {"id": self._request_id, "method": method, "params": params}
        started = time.perf_counter()
        try:
            response = self._session.post(
                self.url,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TdxQuantProbeError(f"{method}: HTTP 请求失败: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        if not isinstance(body, dict):
            raise TdxQuantProbeError(f"{method}: 返回值不是 JSON 对象")
        if body.get("error"):
            raise TdxQuantProbeError(f"{method}: JSON-RPC 错误: {body['error']}")

        result = body.get("result")
        if isinstance(result, dict):
            error_id = str(result.get("ErrorId", "0"))
            if error_id != "0":
                message = result.get("Error") or result.get("Msg") or "未知错误"
                raise TdxQuantProbeError(
                    f"{method}: TdxQuant ErrorId={error_id}: {message}"
                )
        return RpcResult(method=method, elapsed_ms=elapsed_ms, value=result)


def _configure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")


def _result_value(result: Any) -> Any:
    if isinstance(result, dict) and "Value" in result:
        return result["Value"]
    return result


def _find_symbols(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if "." in item and 4 <= len(item) <= 24:
                found.append(item.upper())
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and "." in key:
                    found.append(key.upper())
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(found))


def _row_count(stock_data: Any) -> int:
    if not isinstance(stock_data, dict):
        return 0
    lengths = [len(value) for value in stock_data.values() if isinstance(value, list)]
    return max(lengths, default=0)


def _range_text(stock_data: Any) -> str:
    if not isinstance(stock_data, dict):
        return "无数据"
    dates = stock_data.get("Date") or []
    times = stock_data.get("Time") or []
    if not dates:
        return "无数据"
    first = str(dates[0])
    last = str(dates[-1])
    if times and len(times) == len(dates) and any(str(item) != "0" for item in times):
        first = f"{first} {times[0]}"
        last = f"{last} {times[-1]}"
    return first if first == last else f"{first} .. {last}"


def _summarize_kline(result: RpcResult, symbols: Iterable[str], period: str) -> None:
    value = _result_value(result.value)
    value = value if isinstance(value, dict) else {}
    print(f"[{period}] {result.elapsed_ms:.1f} ms")
    for symbol in symbols:
        stock_data = value.get(symbol, {})
        print(
            f"  {symbol}: {_row_count(stock_data)} 条, "
            f"{_range_text(stock_data)}"
        )


def _probe_kline_one_by_one(
    client: TdxQuantHttpClient,
    symbols: Iterable[str],
    *,
    period: str,
    count: int,
) -> RpcResult | None:
    """Read K-lines one symbol per call as recommended by TdxQuant."""
    print(f"[{period}]")
    last_result: RpcResult | None = None
    for symbol in symbols:
        result = client.call(
            "get_market_data",
            stock_list=[symbol],
            period=period,
            count=count,
            dividend_type="none",
            fill_data=False,
        )
        last_result = result
        value = _result_value(result.value)
        stock_data = value.get(symbol, {}) if isinstance(value, dict) else {}
        print(
            f"  {symbol}: {_row_count(stock_data)} 条, "
            f"{_range_text(stock_data)}, {result.elapsed_ms:.1f} ms"
        )
    return last_result


def _minute_audit(
    client: TdxQuantHttpClient,
    symbols: Iterable[str],
    count: int,
) -> None:
    """Audit local 1-minute coverage one symbol at a time.

    TDX stores no-trade placeholders with zero volume in some .lc1 files even
    when fill_data=False.  They must not silently become executable bars.
    """
    print("[1分钟质量审计]")
    for symbol in symbols:
        result = client.call(
            "get_market_data",
            stock_list=[symbol],
            period="1m",
            count=min(max(int(count), 1), 24000),
            dividend_type="none",
            fill_data=False,
        )
        value = _result_value(result.value)
        stock_data = value.get(symbol, {}) if isinstance(value, dict) else {}
        dates = [str(item) for item in stock_data.get("Date", [])]
        times = [str(item) for item in stock_data.get("Time", [])]
        volumes = stock_data.get("Volume", [])
        row_count = min(len(dates), len(times), len(volumes))
        per_day = Counter(dates[:row_count])
        duplicates = row_count - len(set(zip(dates[:row_count], times[:row_count])))
        zero_by_event = {}
        for event in ("95000", "100000"):
            indexes = [index for index, label in enumerate(times[:row_count]) if label == event]
            zero_by_event[event] = sum(
                Decimal(str(volumes[index])) == 0 for index in indexes
            )
        no_trade_by_1000 = 0
        for trading_date in per_day:
            indexes = [
                index
                for index, (date_value, time_value) in enumerate(
                    zip(dates[:row_count], times[:row_count])
                )
                if date_value == trading_date and time_value <= "100000"
            ]
            if indexes and all(
                Decimal(str(volumes[index])) == 0 for index in indexes
            ):
                no_trade_by_1000 += 1
        day_counts = list(per_day.values())
        print(
            f"  {symbol}: {row_count} 条, {len(per_day)} 日, "
            f"{(dates[0] if dates else '无')} "
            f"{(times[0] if times else '')} .. "
            f"{(dates[-1] if dates else '无')} "
            f"{(times[-1] if times else '')}, "
            f"每日={min(day_counts) if day_counts else 0}.."
            f"{max(day_counts) if day_counts else 0}, "
            f"重复={duplicates}, 09:50零成交={zero_by_event['95000']}, "
            f"10:00零成交={zero_by_event['100000']}, "
            f"10:00前整段无成交={no_trade_by_1000}, "
            f"{result.elapsed_ms:.1f} ms"
        )


def _probe_universe(client: TdxQuantHttpClient) -> tuple[list[str], list[str]]:
    results: dict[str, list[str]] = {}
    for market, label in (("50", "沪深A股"), ("53", "北交所")):
        result = client.call("get_stock_list", market=market, list_type=1)
        symbols = _find_symbols(_result_value(result.value))
        results[market] = symbols
        suffixes = sorted({item.rsplit(".", 1)[-1] for item in symbols})
        print(
            f"[代码表] {label}: {len(symbols)} 个标准代码, "
            f"后缀={suffixes or ['未识别']}, {result.elapsed_ms:.1f} ms"
        )
    return results["50"], results["53"]


def _probe_calendar(client: TdxQuantHttpClient) -> None:
    result = client.call(
        "get_trading_dates",
        market="SH",
        start_time="20260101",
        end_time="",
        count=10,
    )
    if isinstance(result.value, dict):
        dates = result.value.get("Date") or []
    else:
        dates = result.value if isinstance(result.value, list) else []
    print(
        f"[交易日历] {len(dates)} 条, "
        f"{(dates[0] if dates else '无')} .. {(dates[-1] if dates else '无')}, "
        f"{result.elapsed_ms:.1f} ms"
    )
    if not dates:
        print("  提示：需先在通达信下载上证指数 999999 的日线盘后数据。")


def _probe_actions(client: TdxQuantHttpClient, symbol: str) -> None:
    result = client.call(
        "get_divid_factors",
        stock_code=symbol,
        start_time="20000101",
        end_time="",
    )
    if isinstance(result.value, dict):
        dates = result.value.get("Date") or []
        values = result.value.get("Value") or []
        count = max(len(dates), len(values))
        date_range = f"{dates[0]} .. {dates[-1]}" if dates else "无日期"
    elif isinstance(result.value, list):
        count = len(result.value)
        date_range = "已返回列表"
    else:
        count = 0
        date_range = "无数据"
    print(
        f"[公司行动] {symbol}: {count} 条, {date_range}, "
        f"{result.elapsed_ms:.1f} ms"
    )


def _probe_snapshot(client: TdxQuantHttpClient, symbols: Iterable[str]) -> None:
    latencies: list[float] = []
    for symbol in symbols:
        result = client.call("get_market_snapshot", stock_code=symbol, field_list=[])
        latencies.append(result.elapsed_ms)
        value = _result_value(result.value)
        fields = len(value) if isinstance(value, dict) else 0
        print(f"[实时快照] {symbol}: {fields} 个字段, {result.elapsed_ms:.1f} ms")
    if latencies:
        print(
            f"[实时快照延迟] median={statistics.median(latencies):.1f} ms, "
            f"max={max(latencies):.1f} ms"
        )


def _refresh_sample(
    client: TdxQuantHttpClient,
    symbols: list[str],
    periods: Iterable[str],
) -> None:
    for period in periods:
        result = client.call("refresh_kline", stock_list=symbols, period=period)
        print(
            f"[刷新缓存] {period} {','.join(symbols)}: "
            f"完成, {result.elapsed_ms:.1f} ms"
        )
    result = client.call(
        "refresh_kline",
        stock_list=["999999.SH"],
        period="1d",
    )
    print(f"[刷新交易日历基础数据] 999999.SH: 完成, {result.elapsed_ms:.1f} ms")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="探测本机 TdxQuant HTTP 数据接口")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--count", type=int, default=5, help="K线样本条数")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(DEFAULT_SYMBOLS),
        help="沪深样本代码；北交所样本会从代码表自动选择",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="显式刷新样本标的 1d/1m 缓存；会修改通达信本地缓存",
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="额外输出最后一次K线响应，便于排错",
    )
    parser.add_argument(
        "--minute-audit",
        action="store_true",
        help="逐标的审计分钟覆盖、重复和09:50/10:00零成交占位",
    )
    return parser.parse_args()


def main() -> int:
    _configure_stdout()
    args = _parse_args()
    client = TdxQuantHttpClient(args.url, args.timeout)
    print(f"TdxQuant HTTP: {args.url}")

    try:
        _, bj_symbols = _probe_universe(client)
        symbols = list(dict.fromkeys([*args.symbols, *bj_symbols[:1]]))

        if args.refresh:
            # Refresh every explicitly requested sample.  The automatically
            # appended BJ coverage sample remains read-only unless the user
            # also supplied it in --symbols.
            _refresh_sample(
                client,
                list(dict.fromkeys(args.symbols)),
                ("1d", "1m"),
            )

        _probe_kline_one_by_one(
            client,
            symbols,
            period="1d",
            count=args.count,
        )
        minute = _probe_kline_one_by_one(
            client,
            symbols,
            period="1m",
            count=args.count,
        )
        if args.minute_audit:
            _minute_audit(client, args.symbols, args.count)

        _probe_snapshot(client, symbols)
        _probe_actions(client, args.symbols[0])
        _probe_calendar(client)

        if args.raw_json and minute is not None:
            print("[最后一次K线原始响应]")
            print(json.dumps(minute.value, ensure_ascii=False, indent=2)[:20000])
    except TdxQuantProbeError as exc:
        print(f"探测失败：{exc}", file=sys.stderr)
        print(
            "请确认 Windows 通达信已登录，且 127.0.0.1:17709 正在监听；"
            "不要从 WSL 的 Linux Python 直接访问 Windows 回环地址。",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
