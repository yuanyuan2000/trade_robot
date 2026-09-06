from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import json
from typing import Any

import xlwt

from database import backtest_repository


FILTER_COLUMNS = (
    ("high_drawdown", "高点回撤过滤"),
    ("volume_overheat", "放量过热过滤"),
    ("short_momentum", "短期动量过滤"),
    ("single_day_loss", "单日急跌过滤"),
    ("non_positive_trend", "非正长期趋势过滤"),
    ("score_range", "评分区间过滤"),
)

RAPID_DROP_FILTER_COLUMNS = (
    ("percent_drop", "百分比急跌过滤"),
    ("atr_drop", "ATR急跌过滤"),
)


def _all_logs(run_id: int) -> list[dict]:
    result = []
    after = 0
    while True:
        batch = backtest_repository.get_logs(
            run_id,
            level="DEBUG",
            after_sequence=after,
            limit=5000,
        )
        if not batch:
            break
        result.extend(batch)
        after = int(batch[-1]["sequence"])
        if len(batch) < 5000:
            break
    return result


def _safe_cell(value: Any) -> str | float | int:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _styles() -> dict[str, xlwt.XFStyle]:
    header = xlwt.easyxf(
        "font: bold on, colour white;"
        "pattern: pattern solid, fore_colour dark_blue;"
        "align: horiz center, vert center;"
        "borders: bottom thin, left thin, right thin, top thin;"
    )
    text = xlwt.easyxf("align: vert top; borders: bottom hair;")
    number = xlwt.easyxf(
        "align: vert top; borders: bottom hair;",
        num_format_str="0.000000",
    )
    money = xlwt.easyxf(
        "align: vert top; borders: bottom hair;",
        num_format_str="0.00",
    )
    percent = xlwt.easyxf(
        "align: vert top; borders: bottom hair;",
        num_format_str="0.00%",
    )
    return {
        "header": header,
        "text": text,
        "number": number,
        "money": money,
        "percent": percent,
    }


def _write_table(
    sheet,
    headers: list[str],
    rows: list[list[Any]],
    *,
    styles: dict[str, xlwt.XFStyle],
    money_columns: set[str] | None = None,
    percent_columns: set[str] | None = None,
) -> None:
    money_columns = money_columns or set()
    percent_columns = percent_columns or set()
    sheet.panes_frozen = True
    sheet.horz_split_pos = 1
    sheet.remove_splits = True
    for column, header in enumerate(headers):
        sheet.write(0, column, header, styles["header"])
        sheet.col(column).width = min(16000, max(3000, len(header) * 700))
    for row_index, values in enumerate(rows, start=1):
        for column, value in enumerate(values):
            header = headers[column]
            style = styles["text"]
            if header in money_columns:
                style = styles["money"]
            elif header in percent_columns:
                style = styles["percent"]
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                style = styles["number"]
            sheet.write(row_index, column, _safe_cell(value), style)


def _trade_rows(trades: list[dict], logs: list[dict]) -> tuple[list[str], list[list[Any]]]:
    trade_logs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for log in logs:
        if log.get("event_type") == "TRADE":
            trade_logs[(log.get("event_time"), log.get("symbol"))].append(log)
    headers = [
        "序号", "日志等级", "日志类型", "时间", "交易日期", "标的", "操作",
        "数量", "参考点位", "成交点位", "成交金额", "手续费", "滑点成本",
        "已实现PnL", "成交后现金", "成交后持仓数量", "成交后持仓市值",
        "成交后策略仓位", "成交后敞口", "交易原因", "日志消息",
    ]
    rows = []
    for index, trade in enumerate(trades, start=1):
        key = (trade.get("event_time"), trade.get("symbol"))
        log = trade_logs[key].pop(0) if trade_logs[key] else {}
        side = "买入" if trade.get("side") == "BUY" else "卖出"
        event_time = str(trade.get("event_time") or "")
        rows.append(
            [
                index, log.get("level", "INFO"), log.get("event_type", "TRADE"),
                event_time, event_time[:10], trade.get("symbol"), side,
                trade.get("quantity"), trade.get("reference_price"),
                trade.get("fill_price"), trade.get("gross_amount"),
                trade.get("commission"), trade.get("slippage_amount"),
                trade.get("realized_pnl") if trade.get("side") == "SELL" else None,
                trade.get("cash_after"), trade.get("position_quantity_after"),
                trade.get("position_value_after"),
                trade.get("strategy_position_weight_after"),
                trade.get("position_exposure_after", trade.get("position_weight_after")),
                trade.get("reason"), log.get("message"),
            ]
        )
    return headers, rows


def _symbol_pnl_rows(run: dict, trades: list[dict]) -> tuple[list[str], list[list[Any]]]:
    """Summarize per-symbol fills and FIFO realized PnL from sell trades."""
    snapshot_symbols = [
        item.get("symbol")
        for item in (run.get("strategy_snapshot") or {}).get("definition", {}).get("symbols", [])
        if item.get("symbol")
    ]
    trade_symbols = [trade.get("symbol") for trade in trades if trade.get("symbol")]
    symbols = list(dict.fromkeys([*snapshot_symbols, *trade_symbols]))
    headers = [
        "标的", "成交次数", "已实现盈亏总额",
        "最大一次盈利时间", "最大一次盈利金额",
        "最大一次亏损时间", "最大一次亏损金额",
    ]
    rows = []
    for symbol in symbols:
        symbol_trades = [trade for trade in trades if trade.get("symbol") == symbol]
        pnl_trades = [
            trade
            for trade in symbol_trades
            if trade.get("side") == "SELL" and trade.get("realized_pnl") is not None
        ]
        profits = [trade for trade in pnl_trades if float(trade["realized_pnl"]) > 0]
        losses = [trade for trade in pnl_trades if float(trade["realized_pnl"]) < 0]
        largest_profit = max(profits, key=lambda trade: float(trade["realized_pnl"]), default=None)
        largest_loss = min(losses, key=lambda trade: float(trade["realized_pnl"]), default=None)
        rows.append(
            [
                symbol,
                len(symbol_trades),
                sum(float(trade["realized_pnl"]) for trade in pnl_trades),
                largest_profit.get("event_time") if largest_profit else None,
                largest_profit.get("realized_pnl") if largest_profit else None,
                largest_loss.get("event_time") if largest_loss else None,
                largest_loss.get("realized_pnl") if largest_loss else None,
            ]
        )
    return headers, rows


def _sevenstar_rows(
    run: dict,
    trades: list[dict],
    logs: list[dict],
    equity_points: list[dict],
) -> tuple[list[str], list[list[Any]]]:
    snapshot = run.get("strategy_snapshot") or {}
    params = snapshot.get("definition", {}).get("params", {})
    formula_mode = params.get("trend_formula_mode") or (
        "legacy_v1" if snapshot.get("code_version") == "1.0.0" else "consistent_w2"
    )
    formula_label = {
        "consistent_w2": "一致加权 R²",
        "legacy_v1": "历史 v1.0.0 不一致权重",
    }.get(formula_mode, formula_mode)
    symbols = [
        item["symbol"]
        for item in snapshot.get("definition", {}).get("symbols", [])
    ]
    evaluations: dict[str, dict[str, dict]] = defaultdict(dict)
    for log in logs:
        if log.get("event_type") != "SEVENSTAR_DAILY_SCORE":
            continue
        context = log.get("context") or {}
        trading_date = str(log.get("event_time") or "")[:10]
        symbol = log.get("symbol") or context.get("etf")
        if trading_date and symbol:
            evaluations[trading_date][symbol] = context

    trades_by_date: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        trades_by_date[str(trade.get("event_time") or "")[:10]].append(trade)
    equity_by_date = {
        point["trading_date"]: point
        for point in equity_points
    }
    dates = sorted(set(evaluations) | set(trades_by_date) | set(equity_by_date))
    headers = ["日期", "趋势公式", *[f"评分_{symbol}" for symbol in symbols]]
    headers.extend(label for _, label in FILTER_COLUMNS)
    headers.extend(
        [
            "合格排名", "最终目标", "收盘持仓", "当日权益", "累计收益率",
            "最终操作", "操作点位", "操作标的", "卖出PnL",
        ]
    )
    rows = []
    for trading_date in dates:
        day_evaluations = evaluations.get(trading_date, {})
        values: list[Any] = [trading_date, formula_label]
        values.extend(
            day_evaluations.get(symbol, {}).get("score")
            for symbol in symbols
        )
        for code, _label in FILTER_COLUMNS:
            values.append(
                "、".join(
                    symbol
                    for symbol in symbols
                    if code in day_evaluations.get(symbol, {}).get("filter_codes", [])
                )
            )
        ranked = sorted(
            (
                (int(item["rank"]), symbol)
                for symbol, item in day_evaluations.items()
                if item.get("rank") is not None
            ),
        )
        targets = [
            symbol
            for symbol in symbols
            if day_evaluations.get(symbol, {}).get("selected_for_target")
        ]
        point = equity_by_date.get(trading_date, {})
        positions = point.get("positions") or {}
        day_trades = trades_by_date.get(trading_date, [])
        if day_trades:
            actions = ["买入" if item["side"] == "BUY" else "卖出" for item in day_trades]
            action_symbols = [item["symbol"] for item in day_trades]
            action_prices = [f"{float(item['fill_price']):.6f}" for item in day_trades]
            sell_pnl_values = [
                float(item["realized_pnl"])
                for item in day_trades
                if item["side"] == "SELL" and item.get("realized_pnl") is not None
            ]
            sell_pnl = sum(sell_pnl_values) if sell_pnl_values else None
        elif positions:
            actions = ["持有"]
            action_symbols = list(positions)
            action_prices = [
                f"{float(positions[symbol]['price']):.6f}"
                for symbol in action_symbols
            ]
            sell_pnl = None
        else:
            actions, action_symbols, action_prices, sell_pnl = ["持有现金"], ["现金"], [], None
        values.extend(
            [
                "；".join(f"{rank}:{symbol}" for rank, symbol in ranked),
                "、".join(targets),
                "、".join(positions),
                point.get("equity"),
                point.get("return_rate"),
                "；".join(actions),
                "；".join(action_prices),
                "；".join(action_symbols),
                sell_pnl,
            ]
        )
        rows.append(values)
    return headers, rows


def _rapid_drop_rows(
    run: dict,
    trades: list[dict],
    logs: list[dict],
    equity_points: list[dict],
) -> tuple[list[str], list[list[Any]]]:
    snapshot = run.get("strategy_snapshot") or {}
    symbols = [
        item["symbol"]
        for item in snapshot.get("definition", {}).get("symbols", [])
    ]
    evaluations: dict[str, dict[str, dict]] = defaultdict(dict)
    for log in logs:
        if log.get("event_type") != "RAPID_DROP_ATR_DAILY_SCORE":
            continue
        context = log.get("context") or {}
        trading_date = str(log.get("event_time") or "")[:10]
        symbol = log.get("symbol") or context.get("symbol")
        if trading_date and symbol:
            evaluations[trading_date][symbol] = context

    trades_by_date: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        trades_by_date[str(trade.get("event_time") or "")[:10]].append(trade)
    equity_by_date = {point["trading_date"]: point for point in equity_points}
    dates = sorted(set(evaluations) | set(trades_by_date) | set(equity_by_date))
    headers = ["日期"]
    for symbol in symbols:
        headers.extend((f"{symbol}评分公式", f"{symbol}评分结果"))
    headers.extend(label for _code, label in RAPID_DROP_FILTER_COLUMNS)
    headers.extend(("最终操作", "操作点位", "操作标的", "PnL"))

    rows = []
    for trading_date in dates:
        day_evaluations = evaluations.get(trading_date, {})
        values: list[Any] = [trading_date]
        for symbol in symbols:
            item = day_evaluations.get(symbol, {})
            values.extend((item.get("score_formula"), item.get("score")))
        for code, _label in RAPID_DROP_FILTER_COLUMNS:
            values.append(
                "、".join(
                    symbol
                    for symbol in symbols
                    if code in day_evaluations.get(symbol, {}).get("filter_codes", [])
                )
            )
        day_trades = trades_by_date.get(trading_date, [])
        point = equity_by_date.get(trading_date, {})
        positions = point.get("positions") or {}
        if day_trades:
            actions = ["买入" if item["side"] == "BUY" else "卖出" for item in day_trades]
            action_prices = [f"{float(item['fill_price']):.6f}" for item in day_trades]
            action_symbols = [item["symbol"] for item in day_trades]
            sell_pnl_values = [
                float(item["realized_pnl"])
                for item in day_trades
                if item["side"] == "SELL" and item.get("realized_pnl") is not None
            ]
            sell_pnl = sum(sell_pnl_values) if sell_pnl_values else None
        elif positions:
            actions = ["持有"]
            action_symbols = list(positions)
            action_prices = [
                f"{float(positions[symbol]['price']):.6f}"
                for symbol in action_symbols
            ]
            sell_pnl = None
        else:
            actions, action_prices, action_symbols, sell_pnl = ["持有现金"], [], ["现金"], None
        values.extend(
            (
                "；".join(actions),
                "；".join(action_prices),
                "；".join(action_symbols),
                sell_pnl,
            )
        )
        rows.append(values)
    return headers, rows


def _generic_custom_rows(logs: list[dict]) -> tuple[list[str], list[list[Any]]]:
    custom_logs = [
        log for log in logs
        if log.get("event_type") not in {
            "RUN_START", "RUN_COMPLETE", "DAILY_SNAPSHOT", "TRADE",
            "RULE_EVALUATION", "ORDER_REJECTED",
        }
    ]
    context_keys = sorted(
        {
            key
            for log in custom_logs
            for key in (log.get("context") or {})
        }
    )
    headers = ["日志等级", "时间", "日志类型", "标的", "消息", *context_keys]
    rows = [
        [
            log.get("level"), log.get("event_time"), log.get("event_type"),
            log.get("symbol"), log.get("message"),
            *[(log.get("context") or {}).get(key) for key in context_keys],
        ]
        for log in custom_logs
    ]
    return headers, rows


def build_run_xls(run_id: int) -> bytes:
    run = backtest_repository.get_run(run_id)
    trades = backtest_repository.get_trades(run_id)
    logs = _all_logs(run_id)
    equity_points = backtest_repository.get_equity_points(run_id)
    workbook = xlwt.Workbook(encoding="utf-8")
    styles = _styles()

    settings = run.get("settings") or {}
    metrics = run.get("metrics") or {}
    liquidation = metrics.get("liquidation") or {}
    summary_sheet = workbook.add_sheet("运行摘要")
    _write_table(
        summary_sheet,
        ["运行ID", "运行状态", "终止原因", "杠杆倍率", "爆仓时间", "初始资金", "期末权益", "总收益率", "运行参数摘要"],
        [[
            run.get("id"), run.get("status"), run.get("termination_reason"),
            settings.get("leverage_multiplier", 1), liquidation.get("liquidation_time"),
            settings.get("initial_capital"), metrics.get("ending_equity"),
            metrics.get("total_return"), run.get("configuration_summary"),
        ]],
        styles=styles,
        money_columns={"初始资金", "期末权益"},
        percent_columns={"总收益率"},
    )

    trade_headers, trade_rows = _trade_rows(trades, logs)
    trade_sheet = workbook.add_sheet("买卖操作")
    _write_table(
        trade_sheet,
        trade_headers,
        trade_rows,
        styles=styles,
        money_columns={
            "成交金额", "手续费", "滑点成本", "已实现PnL", "成交后现金",
            "成交后持仓市值",
        },
        percent_columns={"成交后策略仓位", "成交后敞口"},
    )

    pnl_headers, pnl_rows = _symbol_pnl_rows(run, trades)
    pnl_sheet = workbook.add_sheet("标的盈亏分析")
    _write_table(
        pnl_sheet,
        pnl_headers,
        pnl_rows,
        styles=styles,
        money_columns={"已实现盈亏总额", "最大一次盈利金额", "最大一次亏损金额"},
    )

    snapshot = run.get("strategy_snapshot") or {}
    if snapshot.get("code_key") == "sevenstar_etf_rotation":
        custom_headers, custom_rows = _sevenstar_rows(
            run, trades, logs, equity_points
        )
    elif snapshot.get("code_key") == "rapid_drop_atr_rotation":
        custom_headers, custom_rows = _rapid_drop_rows(
            run, trades, logs, equity_points
        )
    else:
        custom_headers, custom_rows = _generic_custom_rows(logs)
    custom_sheet = workbook.add_sheet("策略自定义日志")
    _write_table(
        custom_sheet,
        custom_headers,
        custom_rows,
        styles=styles,
        money_columns={"当日权益", "卖出PnL", "PnL"},
        percent_columns={"累计收益率"},
    )

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
