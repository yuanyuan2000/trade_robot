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


if __name__ == "__main__":
    unittest.main()
