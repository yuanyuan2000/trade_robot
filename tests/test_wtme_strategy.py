from __future__ import annotations

from datetime import date, timedelta
import unittest

from services.backtest.code_strategies import RapidDropWtmeRotationStrategy
from services.backtest.data import HistoricalDataSet, _epoch_minute
from services.backtest.engine import BacktestEngine


def business_dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def daily_rows(dates: list[str], closes: list[float]) -> list[dict]:
    rows = []
    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        previous = closes[index - 1] if index else close
        rows.append({
            "date": trading_date,
            "open": previous,
            "high": max(previous, close),
            "low": min(previous, close),
            "close": close,
            "volume": 1000,
        })
    return rows


class WtmeStrategyTests(unittest.TestCase):
    def test_strategy_parameters_cover_wtme_filters_and_times(self) -> None:
        params = RapidDropWtmeRotationStrategy.validate_params({})

        self.assertEqual(params["wtme_period"], 40)
        self.assertEqual(params["wtme_half_life"], 15.0)
        self.assertEqual(params["wtme_epsilon"], 1e-8)
        self.assertIn("enable_percent_drop_filter", params)
        self.assertNotIn("enable_atr_drop_filter", params)
        self.assertNotIn("drop_threshold_atr", params)
        self.assertNotIn("atr_period", params)
        self.assertNotIn("atr_weighting", params)
        self.assertEqual(params["risk_check_time"], "09:40")
        self.assertEqual(params["selection_time"], "10:00")
        self.assertEqual(
            RapidDropWtmeRotationStrategy.required_events({}),
            ("09:40", "10:00"),
        )

    def test_backtest_filters_rapid_drop_and_buys_highest_remaining_wtme(self) -> None:
        dates = business_dates("2024-01-02", 7)
        trading_date = dates[-1]
        closes = {
            "SPY": [99, 100, 101, 102, 103, 104, 105],
            "GLD": [99, 100, 101, 102, 103, 104, 105],
            "SOXX": [100, 100, 110, 100, 110, 100, 101],
        }
        daily = {
            symbol: daily_rows(dates, values)
            for symbol, values in closes.items()
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in closes}
        for symbol in closes:
            for event, signal in (
                ("09:40", 80.0 if symbol == "SPY" else closes[symbol][-2]),
                ("10:00", closes[symbol][-1]),
            ):
                event_minute = _epoch_minute(trading_date, event)
                for minute_key in (event_minute - 1, event_minute):
                    minute[symbol][minute_key] = {
                        "open": signal,
                        "high": signal,
                        "low": signal,
                        "close": signal,
                    }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=[trading_date],
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        strategy = {
            "name": "WTME 回测集成测试",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": RapidDropWtmeRotationStrategy.key,
            "code_version": RapidDropWtmeRotationStrategy.version,
            "definition": {
                "symbols": [
                    {"symbol": symbol, "max_weight": 100}
                    for symbol in closes
                ],
                "params": {
                    "wtme_period": 4,
                    "wtme_half_life": 2,
                    "drop_threshold_percent": 10,
                },
            },
            "default_settings": {},
        }
        settings = {
            "start_date": trading_date,
            "end_date": trading_date,
            "initial_capital": 10_000,
            "commission_per_share": 0,
            "minimum_commission": 0,
            "slippage_bps": 0,
            "allow_fractional_shares": True,
            "benchmark": "none",
            "risk_free_rate": 0,
            "strict_data": True,
        }

        result = BacktestEngine(strategy, settings, dataset=dataset).run()

        self.assertEqual(
            [(trade["side"], trade["symbol"]) for trade in result.trades],
            [("BUY", "GLD")],
        )
        score_logs = [
            log for log in result.logs
            if log["event_type"] == "RAPID_DROP_WTME_DAILY_SCORE"
        ]
        by_symbol = {log["symbol"]: log["context"] for log in score_logs}
        self.assertIn("percent_drop", by_symbol["SPY"]["filter_codes"])
        self.assertIsNone(by_symbol["SPY"]["rank"])
        self.assertEqual(by_symbol["GLD"]["rank"], 1)
        self.assertTrue(by_symbol["GLD"]["selected_for_target"])
        self.assertGreater(by_symbol["GLD"]["score"], by_symbol["SOXX"]["score"])
        self.assertTrue(by_symbol["GLD"]["current_observation_is_partial"])
        self.assertIsNotNone(by_symbol["GLD"]["weighted_return"])
        self.assertIsNotNone(by_symbol["GLD"]["weighted_true_range"])

    def test_visual_strategy_matches_code_strategy_decisions_and_results(self) -> None:
        dates = business_dates("2024-01-02", 10)
        run_dates = dates[-3:]
        closes = {
            "SPY": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
            "GLD": [100, 99, 101, 99, 102, 100, 103, 101, 104, 102],
            "SOXX": [100, 100, 101, 100, 101, 100, 101, 100, 101, 100],
        }
        daily = {
            symbol: daily_rows(dates, values)
            for symbol, values in closes.items()
        }
        minute: dict[str, dict[int, dict]] = {symbol: {} for symbol in closes}
        selection_prices = [
            {"SPY": 112, "GLD": 102, "SOXX": 101},
            {"SPY": 115, "GLD": 108, "SOXX": 102},
            {"SPY": 118, "GLD": 103, "SOXX": 101},
        ]
        for day_index, trading_date in enumerate(run_dates):
            for symbol in closes:
                previous_close = float(daily[symbol][dates.index(trading_date) - 1]["close"])
                risk_price = (
                    previous_close * 0.75
                    if day_index == 1 and symbol == "SPY"
                    else previous_close
                )
                for event, signal in (
                    ("09:40", risk_price),
                    ("10:00", selection_prices[day_index][symbol]),
                ):
                    event_minute = _epoch_minute(trading_date, event)
                    for minute_key in (event_minute - 1, event_minute):
                        minute[symbol][minute_key] = {
                            "open": signal,
                            "high": signal,
                            "low": signal,
                            "close": signal,
                        }
        dataset = HistoricalDataSet(
            daily=daily,
            sessions=run_dates,
            minute=minute,
            required_intraday_events=["09:40", "10:00"],
        )
        symbols = [
            {"symbol": symbol, "max_weight": 100, "leverage_multiplier": 2}
            for symbol in closes
        ]
        params = {
            "wtme_period": 4,
            "wtme_half_life": 2,
            "wtme_epsilon": 1e-8,
            "enable_percent_drop_filter": True,
            "drop_threshold_percent": 10,
            "drop_lookback_sessions": 2,
            "risk_check_time": "09:40",
            "selection_time": "10:00",
            "target_weight": 100,
        }
        code_strategy = {
            "name": "代码 WTME 策略",
            "design_mode": "code",
            "selection_mode": "competition",
            "code_key": RapidDropWtmeRotationStrategy.key,
            "code_version": RapidDropWtmeRotationStrategy.version,
            "definition": {"symbols": symbols, "params": params},
            "default_settings": {},
        }
        visual_strategy = {
            "name": "非代码 WTME 策略",
            "design_mode": "visual",
            "selection_mode": "competition",
            "definition": {
                "symbols": symbols,
                "rules": [{
                    "id": "rapid-drop-exit",
                    "name": "急跌时退出并当日回避",
                    "enabled": True,
                    "priority": 10,
                    "action": "SELL",
                    "sizing_mode": "TARGET",
                    "value": 0,
                    "condition": "rapid_drop(2, 10) = 1",
                    "when": "09:40",
                }],
                "competition": {
                    "eligibility": "rapid_drop(2, 10) = 0",
                    "eligibility_when": "09:40",
                    "score": "wtme(4, 2, 0.00000001)",
                    "minimum_score": None,
                    "target_weight": 100,
                    "cash_when_none": True,
                    "rebalance_existing": False,
                    "when": "10:00",
                },
            },
            "default_settings": {},
        }
        run_settings = {
            "start_date": run_dates[0],
            "end_date": run_dates[-1],
            "initial_capital": 10_000,
            "leverage_multiplier": 1,
            "commission_per_share": 0.01,
            "minimum_commission": 1,
            "slippage_bps": 0,
            "allow_fractional_shares": True,
            "benchmark": "none",
            "risk_free_rate": 0,
            "strict_data": True,
        }

        code_result = BacktestEngine(
            code_strategy, run_settings, dataset=dataset
        ).run()
        visual_result = BacktestEngine(
            visual_strategy, run_settings, dataset=dataset
        ).run()

        trade_fields = (
            "event_time", "symbol", "side", "quantity",
            "reference_price", "fill_price", "gross_amount", "commission",
            "slippage_amount", "realized_pnl", "position_quantity_after",
            "position_value_after", "position_weight_after", "cash_after",
        )
        self.assertEqual(
            [tuple(trade[field] for field in trade_fields) for trade in visual_result.trades],
            [tuple(trade[field] for field in trade_fields) for trade in code_result.trades],
        )
        self.assertEqual(visual_result.equity_points, code_result.equity_points)
        self.assertEqual(visual_result.metrics, code_result.metrics)


if __name__ == "__main__":
    unittest.main()
