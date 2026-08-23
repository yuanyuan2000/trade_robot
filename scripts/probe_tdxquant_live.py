r"""Measure TdxQuant quote freshness around CN strategy event times.

This is a diagnostic script.  It does not write application databases and it
does not place orders.  Run it with the Windows project interpreter while the
TdxW client is logged in, starting shortly before the first event::

    .venv\Scripts\python.exe scripts\probe_tdxquant_live.py ^
      --duration-seconds 720 --jsonl data\tdx_live_probe.jsonl

The TdxQuant subscription callback currently carries only the security code,
so the script separately polls the HTTP quote APIs and records local receipt
times.  Those receipt times measure the usable application contract; they are
not exchange timestamps.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import importlib
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.probe_tdxquant import (
        DEFAULT_URL,
        TdxQuantHttpClient,
        TdxQuantProbeError,
        _configure_stdout,
        _result_value,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from probe_tdxquant import (
        DEFAULT_URL,
        TdxQuantHttpClient,
        TdxQuantProbeError,
        _configure_stdout,
        _result_value,
    )


DEFAULT_SYMBOLS = (
    "518880.SH",
    "161226.SZ",
    "501018.SH",
    "513100.SH",
    "159915.SZ",
)
SHANGHAI_EVENT_TIMES = ("09:50", "10:00")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="量化 TdxQuant 在 09:50/10:00 附近的实时行情新鲜度"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--event-window-seconds", type=float, default=10.0)
    parser.add_argument("--events", nargs="+", default=list(SHANGHAI_EVENT_TIMES))
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--tdx-user-dir",
        default=r"E:\ProgramFiles\TDX\PYPlugins\user",
        help="包含 tqcenter.py 的 Windows 目录",
    )
    parser.add_argument(
        "--jsonl",
        help="可选诊断输出文件；每行一个 JSON 对象，不涉及业务数据库",
    )
    return parser.parse_args()


def _local_now() -> datetime:
    return datetime.now(SHANGHAI)


def _event_datetimes(now: datetime, values: list[str]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for value in values:
        try:
            hour, minute = (int(item) for item in value.split(":"))
            result[value] = now.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无效事件时间：{value}") from exc
    return result


class Recorder:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path).resolve() if path else None
        self._handle = None
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    def __enter__(self) -> "Recorder":
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle:
            self._handle.close()

    def add(self, kind: str, **payload: Any) -> dict[str, Any]:
        record = {
            "kind": kind,
            "received_at": _local_now().isoformat(),
            "monotonic": time.monotonic(),
            **payload,
        }
        with self._lock:
            self.records.append(record)
            if self._handle:
                self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._handle.flush()
        return record


def _load_tq(user_dir: str):
    normalized = str(user_dir).strip()
    if not normalized:
        raise ValueError("tdx-user-dir 不能为空")
    if normalized not in sys.path:
        sys.path.insert(0, normalized)
    return importlib.import_module("tqcenter").tq


def _is_event_window(
    now: datetime,
    events: dict[str, datetime],
    window_seconds: float,
) -> list[str]:
    return [
        label
        for label, target in events.items()
        if abs((now - target).total_seconds()) <= window_seconds
    ]


def _snapshot_all(
    client: TdxQuantHttpClient,
    recorder: Recorder,
    symbols: list[str],
    event_labels: list[str],
) -> None:
    for symbol in symbols:
        try:
            result = client.call(
                "get_market_snapshot",
                stock_code=symbol,
                field_list=[],
            )
            value = _result_value(result.value)
            recorder.add(
                "snapshot",
                symbol=symbol,
                events=event_labels,
                elapsed_ms=round(result.elapsed_ms, 3),
                data=value,
            )
        except TdxQuantProbeError as exc:
            recorder.add(
                "snapshot_error",
                symbol=symbol,
                events=event_labels,
                error=str(exc),
            )


def _probe_hq_dates(
    client: TdxQuantHttpClient,
    recorder: Recorder,
    symbols: list[str],
    event: str,
) -> None:
    for symbol in symbols:
        try:
            result = client.call(
                "get_more_info",
                stock_code=symbol,
                field_list=["HqDate", "IsT0Fund", "ZTPrice", "DTPrice"],
            )
            recorder.add(
                "instrument_state",
                symbol=symbol,
                event=event,
                elapsed_ms=round(result.elapsed_ms, 3),
                data=_result_value(result.value),
            )
        except TdxQuantProbeError as exc:
            recorder.add(
                "instrument_state_error",
                symbol=symbol,
                event=event,
                error=str(exc),
            )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def _print_summary(
    recorder: Recorder,
    symbols: list[str],
    events: dict[str, datetime],
) -> None:
    callbacks: dict[str, list[dict]] = defaultdict(list)
    snapshots: dict[str, list[dict]] = defaultdict(list)
    pricevol_latencies: list[float] = []
    snapshot_latencies: list[float] = []
    for row in recorder.records:
        if row["kind"] == "callback":
            callbacks[str(row.get("symbol"))].append(row)
        elif row["kind"] == "snapshot":
            snapshots[str(row.get("symbol"))].append(row)
            snapshot_latencies.append(float(row["elapsed_ms"]))
        elif row["kind"] == "pricevol":
            pricevol_latencies.append(float(row["elapsed_ms"]))

    print("\n[汇总]")
    if pricevol_latencies:
        print(
            "get_pricevol ms: "
            f"median={statistics.median(pricevol_latencies):.1f}, "
            f"p95={_percentile(pricevol_latencies, 0.95):.1f}, "
            f"max={max(pricevol_latencies):.1f}"
        )
    if snapshot_latencies:
        print(
            "snapshot ms: "
            f"median={statistics.median(snapshot_latencies):.1f}, "
            f"p95={_percentile(snapshot_latencies, 0.95):.1f}, "
            f"max={max(snapshot_latencies):.1f}"
        )

    for symbol in symbols:
        values = callbacks[symbol]
        gaps = [
            values[index]["monotonic"] - values[index - 1]["monotonic"]
            for index in range(1, len(values))
        ]
        gap_text = (
            f"median_gap={statistics.median(gaps):.2f}s, max_gap={max(gaps):.2f}s"
            if gaps
            else "无可计算间隔"
        )
        print(f"callback {symbol}: {len(values)} 次, {gap_text}")

    for label, target in events.items():
        target_epoch = target.timestamp()
        print(f"事件 {label}:")
        for symbol in symbols:
            candidates = snapshots[symbol]
            before = [
                row for row in candidates
                if datetime.fromisoformat(row["received_at"]).timestamp() < target_epoch
            ]
            after = [
                row for row in candidates
                if datetime.fromisoformat(row["received_at"]).timestamp() >= target_epoch
            ]
            signal = before[-1] if before else None
            fill = after[0] if after else None
            def quote_text(row: dict | None) -> str:
                if not row:
                    return "缺失"
                data = row.get("data") or {}
                offset = datetime.fromisoformat(row["received_at"]).timestamp() - target_epoch
                return (
                    f"{offset:+.2f}s Now={data.get('Now')} "
                    f"Volume={data.get('Volume')} "
                    f"bid={data.get('Buyp')} ask={data.get('Sellp')}"
                )
            print(
                f"  {symbol}: event前={quote_text(signal)}; "
                f"event后={quote_text(fill)}"
            )


def main() -> int:
    _configure_stdout()
    args = _parse_args()
    symbols = list(dict.fromkeys(str(item).strip().upper() for item in args.symbols))
    if args.duration_seconds <= 0 or args.poll_seconds <= 0:
        raise ValueError("duration-seconds 和 poll-seconds 必须大于 0")

    now = _local_now()
    events = _event_datetimes(now, list(args.events))
    client = TdxQuantHttpClient(args.url, args.timeout)
    tq = _load_tq(args.tdx_user_dir)
    stop_at = time.monotonic() + args.duration_seconds
    instrument_state_done: set[str] = set()

    with Recorder(args.jsonl) as recorder:
        def callback(payload: str) -> None:
            try:
                value = json.loads(payload)
            except (TypeError, ValueError):
                value = {"raw": str(payload)}
            recorder.add("callback", symbol=value.get("Code"), data=value)

        subscribed = False
        try:
            tq.initialize(Path(__file__).name)
            result = tq.subscribe_hq(stock_list=symbols, callback=callback)
            subscribed = bool(result)
            recorder.add("subscribe", symbols=symbols, result=result)
            print(f"订阅 {len(symbols)} 个标的；采样 {args.duration_seconds:g} 秒")

            while time.monotonic() < stop_at:
                loop_started = time.monotonic()
                current = _local_now()
                active_events = _is_event_window(
                    current,
                    events,
                    args.event_window_seconds,
                )
                try:
                    result = client.call("get_pricevol", stock_list=symbols)
                    recorder.add(
                        "pricevol",
                        events=active_events,
                        elapsed_ms=round(result.elapsed_ms, 3),
                        data=_result_value(result.value),
                    )
                except TdxQuantProbeError as exc:
                    recorder.add("pricevol_error", events=active_events, error=str(exc))

                if active_events:
                    _snapshot_all(client, recorder, symbols, active_events)
                    for label in active_events:
                        if current >= events[label] and label not in instrument_state_done:
                            _probe_hq_dates(client, recorder, symbols, label)
                            instrument_state_done.add(label)

                delay = args.poll_seconds - (time.monotonic() - loop_started)
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt:
            recorder.add("interrupted")
        finally:
            if subscribed:
                try:
                    result = tq.unsubscribe_hq(stock_list=symbols)
                    recorder.add("unsubscribe", symbols=symbols, result=result)
                except Exception as exc:  # diagnostic cleanup must continue
                    recorder.add("unsubscribe_error", error=str(exc))
            try:
                tq.close()
            except Exception as exc:  # diagnostic cleanup must continue
                recorder.add("close_error", error=str(exc))

        _print_summary(recorder, symbols, events)
        if recorder.path:
            print(f"原始诊断记录：{recorder.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
