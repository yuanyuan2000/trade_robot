from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xlrd

import database.db as main_db
from database import backtest_repository
from services.backtest.export import build_run_xls


class BacktestExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "market.sqlite"
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.patchers = [
            patch.object(main_db, "DATABASE_PATH", self.database_path),
            patch.object(main_db, "DATA_DIR", self.data_dir),
        ]
        for patcher in self.patchers:
            patcher.start()
        main_db.init_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_sevenstar_xls_has_trade_and_daily_analysis_sheets(self) -> None:
        strategy = backtest_repository.create_strategy(
            {
                "name": "七星导出测试",
                "description": "test",
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": "sevenstar_etf_rotation",
                "code_version": "1.0.0",
                "definition": {
                    "symbols": [
                        {"symbol": "SPY", "max_weight": 100},
                        {"symbol": "QQQ", "max_weight": 100},
                    ],
                    "params": {},
                },
                "default_settings": {},
                "schema_version": 1,
            }
        )
        run = backtest_repository.create_run(strategy, {})
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[
                {
                    "trading_date": "2024-01-02", "cash": 1000,
                    "receivables": 0, "positions_value": 0, "equity": 1000,
                    "return_rate": 0.01, "drawdown_rate": 0,
                    "benchmark_equity": None, "benchmark_return_rate": None,
                    "positions": {},
                }
            ],
            trades=[
                {
                    "event_time": "2024-01-02 14:00", "symbol": "SPY",
                    "side": "SELL", "quantity": 2, "reference_price": 501,
                    "fill_price": 500, "gross_amount": 1000, "commission": 1,
                    "slippage_amount": 2, "realized_pnl": 25,
                    "cash_after": 1000, "position_quantity_after": 0,
                    "position_value_after": 0, "position_weight_after": 0,
                    "reason": "七星排名换仓",
                }
            ],
            logs=[
                {
                    "event_time": "2024-01-02 14:00", "level": "INFO",
                    "event_type": "TRADE", "symbol": "SPY", "message": "卖出",
                    "context": {},
                },
                {
                    "event_time": "2024-01-02 14:00", "level": "DEBUG",
                    "event_type": "SEVENSTAR_DAILY_SCORE", "symbol": "SPY",
                    "message": "SPY 评分", "context": {
                        "etf": "SPY", "score": 1.25, "rank": 1,
                        "eligible": True, "selected_for_target": True,
                        "filter_codes": [], "filter_reasons": [],
                    },
                },
                {
                    "event_time": "2024-01-02 14:00", "level": "DEBUG",
                    "event_type": "SEVENSTAR_DAILY_SCORE", "symbol": "QQQ",
                    "message": "QQQ 评分", "context": {
                        "etf": "QQQ", "score": -0.5, "rank": None,
                        "eligible": False, "selected_for_target": False,
                        "filter_codes": ["short_momentum", "non_positive_trend"],
                        "filter_reasons": ["短期动量不足", "长期拟合趋势非正"],
                    },
                },
            ],
        )

        workbook = xlrd.open_workbook(file_contents=build_run_xls(run["id"]))

        self.assertEqual(workbook.sheet_names(), ["买卖操作", "策略自定义日志"])
        trades = workbook.sheet_by_name("买卖操作")
        trade_headers = trades.row_values(0)
        self.assertEqual(trades.cell_value(1, trade_headers.index("操作")), "卖出")
        self.assertEqual(trades.cell_value(1, trade_headers.index("已实现PnL")), 25)
        custom = workbook.sheet_by_name("策略自定义日志")
        custom_headers = custom.row_values(0)
        self.assertEqual(
            custom.cell_value(1, custom_headers.index("趋势公式")),
            "历史 v1.0.0 不一致权重",
        )
        self.assertEqual(custom.cell_value(1, custom_headers.index("评分_SPY")), 1.25)
        self.assertEqual(
            custom.cell_value(1, custom_headers.index("短期动量过滤")), "QQQ"
        )
        self.assertEqual(
            custom.cell_value(1, custom_headers.index("非正长期趋势过滤")), "QQQ"
        )
        self.assertEqual(custom.cell_value(1, custom_headers.index("最终操作")), "卖出")
        self.assertEqual(custom.cell_value(1, custom_headers.index("卖出PnL")), 25)

    def test_rapid_drop_xls_has_daily_formula_filter_action_and_sell_pnl(self) -> None:
        strategy = backtest_repository.create_strategy(
            {
                "name": "急跌轮动导出测试",
                "description": "test",
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": "rapid_drop_atr_rotation",
                "code_version": "1.2.0",
                "definition": {
                    "symbols": [
                        {"symbol": "SPY", "max_weight": 100},
                        {"symbol": "SOXX", "max_weight": 100},
                    ],
                    "params": {},
                },
                "default_settings": {},
                "schema_version": 1,
            }
        )
        run = backtest_repository.create_run(strategy, {})
        backtest_repository.replace_run_output(
            run["id"],
            equity_points=[
                {
                    "trading_date": "2026-04-01", "cash": 1000,
                    "receivables": 0, "positions_value": 0, "equity": 1000,
                    "return_rate": 0, "drawdown_rate": 0,
                    "benchmark_equity": None, "benchmark_return_rate": None,
                    "positions": {},
                }
            ],
            trades=[
                {
                    "event_time": "2026-04-01 09:40", "symbol": "SOXX",
                    "side": "SELL", "quantity": 2, "reference_price": 201,
                    "fill_price": 200, "gross_amount": 400, "commission": 1,
                    "slippage_amount": 0, "realized_pnl": 12,
                    "cash_after": 1000, "position_quantity_after": 0,
                    "position_value_after": 0, "position_weight_after": 0,
                    "reason": "ATR 急跌",
                }
            ],
            logs=[
                {
                    "event_time": "2026-04-01 10:00", "level": "DEBUG",
                    "event_type": "RAPID_DROP_ATR_DAILY_SCORE", "symbol": "SPY",
                    "message": "SPY 评分", "context": {
                        "symbol": "SPY", "score": 1.5,
                        "score_formula": "(103 - 100) / 2",
                        "filter_codes": [],
                    },
                },
                {
                    "event_time": "2026-04-01 10:00", "level": "DEBUG",
                    "event_type": "RAPID_DROP_ATR_DAILY_SCORE", "symbol": "SOXX",
                    "message": "SOXX 评分", "context": {
                        "symbol": "SOXX", "score": -4.0,
                        "score_formula": "(192 - 200) / 2",
                        "filter_codes": ["percent_drop", "atr_drop"],
                    },
                },
            ],
        )

        workbook = xlrd.open_workbook(file_contents=build_run_xls(run["id"]))
        custom = workbook.sheet_by_name("策略自定义日志")
        headers = custom.row_values(0)

        self.assertEqual(custom.cell_value(1, headers.index("日期")), "2026-04-01")
        self.assertEqual(
            custom.cell_value(1, headers.index("SPY评分公式")), "(103 - 100) / 2"
        )
        self.assertEqual(custom.cell_value(1, headers.index("SPY评分结果")), 1.5)
        self.assertEqual(
            custom.cell_value(1, headers.index("百分比急跌过滤")), "SOXX"
        )
        self.assertEqual(custom.cell_value(1, headers.index("ATR急跌过滤")), "SOXX")
        self.assertEqual(custom.cell_value(1, headers.index("最终操作")), "卖出")
        self.assertEqual(custom.cell_value(1, headers.index("操作点位")), "200.000000")
        self.assertEqual(custom.cell_value(1, headers.index("操作标的")), "SOXX")
        self.assertEqual(custom.cell_value(1, headers.index("PnL")), 12)


if __name__ == "__main__":
    unittest.main()
