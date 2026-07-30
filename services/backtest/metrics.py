from __future__ import annotations

from datetime import date
import math
from statistics import fmean, pstdev


TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = 365.2425


def _ratio(numerator: float, denominator: float) -> float | None:
    if abs(denominator) < 1e-15:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def calculate_metrics(
    equity_points: list[dict],
    trades: list[dict],
    *,
    initial_capital: float,
    risk_free_rate: float = 0,
    total_commission: float = 0,
    total_slippage: float = 0,
) -> dict:
    if not equity_points:
        return {}
    equities = [float(point["equity"]) for point in equity_points]
    daily_returns = [equities[0] / float(initial_capital) - 1]
    daily_returns.extend(
        equities[index] / equities[index - 1] - 1
        for index in range(1, len(equities))
        if equities[index - 1] > 0
    )
    ending = equities[-1]
    total_return = ending / float(initial_capital) - 1
    start = date.fromisoformat(equity_points[0]["trading_date"])
    end = date.fromisoformat(equity_points[-1]["trading_date"])
    elapsed_days = max(0, (end - start).days)
    annualized_return = (
        (ending / float(initial_capital)) ** (DAYS_PER_YEAR / elapsed_days) - 1
        if elapsed_days > 0 and ending > 0
        else total_return
    )
    volatility = (
        pstdev(daily_returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if len(daily_returns) >= 2
        else 0.0
    )
    daily_risk_free = (1 + float(risk_free_rate)) ** (
        1 / TRADING_DAYS_PER_YEAR
    ) - 1
    excess = [value - daily_risk_free for value in daily_returns]
    excess_std = pstdev(excess) if len(excess) >= 2 else 0.0
    sharpe = (
        fmean(excess) / excess_std * math.sqrt(TRADING_DAYS_PER_YEAR)
        if excess and excess_std > 1e-15
        else None
    )
    downside = [min(0.0, value - daily_risk_free) for value in daily_returns]
    downside_deviation = (
        math.sqrt(fmean(value * value for value in downside))
        if downside
        else 0.0
    )
    sortino = (
        fmean(excess) / downside_deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
        if excess and downside_deviation > 1e-15
        else None
    )

    running_peak = float(initial_capital)
    running_peak_date = "INITIAL"
    max_drawdown_signed = 0.0
    max_drawdown_peak_date = running_peak_date
    max_drawdown_trough_date = equity_points[0]["trading_date"]
    for point, equity in zip(equity_points, equities):
        if equity > running_peak:
            running_peak = equity
            running_peak_date = point["trading_date"]
        drawdown = equity / running_peak - 1 if running_peak > 0 else 0
        if drawdown < max_drawdown_signed:
            max_drawdown_signed = drawdown
            max_drawdown_peak_date = running_peak_date
            max_drawdown_trough_date = point["trading_date"]
    max_drawdown = abs(max_drawdown_signed)
    sells = [
        float(trade["realized_pnl"])
        for trade in trades
        if trade["side"] == "SELL" and trade.get("realized_pnl") is not None
    ]
    wins = [value for value in sells if value > 0]
    losses = [value for value in sells if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    average_equity = fmean(equities)
    gross_turnover = sum(float(trade["gross_amount"]) for trade in trades)
    exposures = [
        float(point["positions_value"]) / float(point["equity"])
        for point in equity_points
        if float(point["equity"]) > 0
    ]
    benchmark_return = equity_points[-1].get("benchmark_return_rate")

    return {
        "initial_capital": float(initial_capital),
        "ending_equity": ending,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "max_drawdown_signed": max_drawdown_signed,
        "max_drawdown_peak_date": max_drawdown_peak_date,
        "max_drawdown_trough_date": max_drawdown_trough_date,
        "annualized_volatility": volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": _ratio(annualized_return, max_drawdown),
        "trade_count": len(trades),
        "closed_trade_count": len(sells),
        "win_rate": len(wins) / len(sells) if sells else None,
        "average_realized_pnl": fmean(sells) if sells else None,
        "profit_factor": _ratio(gross_profit, gross_loss),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "total_commission": float(total_commission),
        "total_slippage": float(total_slippage),
        "average_exposure": fmean(exposures) if exposures else 0.0,
        "turnover": _ratio(gross_turnover, average_equity),
        "benchmark_total_return": (
            float(benchmark_return) if benchmark_return is not None else None
        ),
        "excess_return": (
            total_return - float(benchmark_return)
            if benchmark_return is not None
            else None
        ),
        "session_count": len(equity_points),
        "elapsed_calendar_days": elapsed_days,
        "calculation": {
            "return_frequency": "daily_close",
            "trading_days_per_year": TRADING_DAYS_PER_YEAR,
            "calendar_days_per_year": DAYS_PER_YEAR,
            "risk_free_rate": float(risk_free_rate),
            "pnl_method": "FIFO including buy and sell commissions",
        },
    }
