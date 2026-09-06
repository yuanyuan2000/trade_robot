import ast
import math
import unittest
from pathlib import Path

import numpy as np

from services.indicator_service import calculate_wtme_components


ROOT = Path(__file__).resolve().parents[1]
JOINQUANT = ROOT / "other_platform" / "joinquant"
FUTU = ROOT / "other_platform" / "futubull"
TDX = ROOT / "other_platform" / "TDX"


class _Column:
    def __init__(self, values):
        self._values = list(values)

    def astype(self, _type):
        return self

    @property
    def values(self):
        return np.asarray(self._values)

    @property
    def iloc(self):
        return self

    def __getitem__(self, index):
        return self._values[index]


class _Frame:
    def __init__(self, rows):
        self._columns = {
            key: _Column([row[key] for row in rows])
            for key in ("high", "low", "close")
        }

    def __len__(self):
        return len(self._columns["close"]._values)

    def __getitem__(self, key):
        return self._columns[key]


def _joinquant_functions(*names):
    path = JOINQUANT / "rapid_drop_wtme_rotation_v2.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "builtins": __import__("builtins"),
        "math": math,
        "np": np,
        "WTME_PERIOD": 4,
        "WTME_HALF_LIFE": 2.0,
        "WTME_EPSILON": 1e-8,
        "ENABLE_VOLAT_DYNAMIC_LEVERAGE": True,
        "VOLATILITY_PERIOD": 4,
        "STRESS_DAYS": 10,
        "MAX_LOSS_PERCENT": 40.0,
        "MAX_DYNAMIC_LEVERAGE": 5.0,
        "ALLOCATION_MODE": "equal",
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace, source


class JoinQuantWtmeV2Tests(unittest.TestCase):
    def test_old_strategy_is_preserved_and_v2_is_parseable(self):
        self.assertTrue((JOINQUANT / "rapid_drop_wtme_rotation.py").exists())
        source = (JOINQUANT / "rapid_drop_wtme_rotation_v2.py").read_text(
            encoding="utf-8"
        )
        ast.parse(source)
        self.assertIn("RapidDropWtmeRotationStrategy 2.0.0", source)
        self.assertIn("REBALANCE_ON_DYNAMIC_LEVERAGE_CHANGE = False", source)

    def test_saved_strategy_profile_is_reproduced(self):
        source = (JOINQUANT / "rapid_drop_wtme_rotation_v2.py").read_text(
            encoding="utf-8"
        )
        expected = (
            'RISK_CHECK_TIME = "09:50"', "WTME_PERIOD = 13",
            "WTME_HALF_LIFE = 6.0", "DROP_LOOKBACK_SESSIONS = 5",
            "DROP_THRESHOLD_PERCENT = 5.0", "BUY_TOP_N = 1",
            'BUY_CONDITION_OPERATOR = "and"', "BUY_SCORE_THRESHOLD = -15.0",
            "MAX_SIMULTANEOUS_HOLDINGS = 1", 'ALLOCATION_MODE = "equal"',
            "VOLATILITY_PERIOD = 15", "STRESS_DAYS = 10",
            "MAX_LOSS_PERCENT = 40.0", "MAX_DYNAMIC_LEVERAGE = 5.0",
        )
        for setting in expected:
            self.assertIn(setting, source)

    def test_wtme_dynamic_leverage_and_one_fifth_mapping(self):
        namespace, _source = _joinquant_functions(
            "_calculate_wtme",
            "_dynamic_leverage",
            "_platform_target_percent",
        )
        rows = [
            {"high": 101.0, "low": 99.0, "close": 100.0},
            {"high": 103.0, "low": 100.0, "close": 102.0},
            {"high": 104.0, "low": 101.0, "close": 103.0},
            {"high": 106.0, "low": 102.0, "close": 105.0},
        ]
        frame = _Frame(rows)
        current = 104.0
        project_rows = [dict(row, date=str(index)) for index, row in enumerate(rows)]
        project_rows.append({
            "date": "current", "high": 105.0, "low": 104.0, "close": current
        })
        expected = calculate_wtme_components(project_rows, 4, 2.0, 1e-8)
        self.assertAlmostEqual(
            namespace["_calculate_wtme"](frame, current),
            expected["value"],
            places=12,
        )

        namespace["_daily_history"] = lambda _security, _count: frame
        leverage = namespace["_dynamic_leverage"]("GLD", current)
        self.assertGreaterEqual(leverage, 1.0)
        self.assertLessEqual(leverage, 5.0)
        self.assertAlmostEqual(leverage * 10, round(leverage * 10), places=10)
        self.assertEqual(
            namespace["_platform_target_percent"](100.0, 1.0, 1),
            20.0,
        )
        self.assertEqual(
            namespace["_platform_target_percent"](100.0, 5.0, 1),
            100.0,
        )


class FutuWtmeV2Tests(unittest.TestCase):
    def test_saved_strategy_profile_is_reproduced(self):
        source = (FUTU / "rapid_drop_wtme_rotation_v2.py").read_text(
            encoding="utf-8"
        )
        expected = (
            "self.risk_check_minute = show_variable(50, GlobalType.INT)",
            "self.wtme_period = show_variable(13, GlobalType.INT)",
            "self.wtme_half_life = show_variable(6.0, GlobalType.FLOAT)",
            "self.drop_lookback_sessions = show_variable(5, GlobalType.INT)",
            "self.drop_threshold_percent = show_variable(5.0, GlobalType.FLOAT)",
            "self.buy_top_n = show_variable(1, GlobalType.INT)",
            "self.buy_condition_operator = show_variable(1, GlobalType.INT)",
            "self.buy_score_threshold = show_variable(-15.0, GlobalType.FLOAT)",
            "self.max_simultaneous_holdings = show_variable(1, GlobalType.INT)",
            "self.allocation_mode = show_variable(1, GlobalType.INT)",
            "self.volatility_period = show_variable(15, GlobalType.INT)",
            "self.stress_days = show_variable(10, GlobalType.INT)",
            "self.max_loss_percent = show_variable(40.0, GlobalType.FLOAT)",
            "self.max_dynamic_leverage = show_variable(5.0, GlobalType.FLOAT)",
        )
        for setting in expected:
            self.assertIn(setting, source)


class TdxUsPoolTests(unittest.TestCase):
    def test_us_pc_dashboard_matches_working_a_share_pc_structure(self):
        source = (TDX / "WTMEPOOLUS.txt").read_text(encoding="utf-8")
        expected = {"DRAM", "FXI", "GLD", "IBIT", "QQQ", "SLV", "SOXX", "SPY", "XLE"}
        for symbol in expected:
            self.assertIn(f"CALCSTOCKINDEX('74_{symbol}','WTMEAPPSORT',1)", source)
            self.assertIn(f"CALCSTOCKINDEX('74_{symbol}','WTMEAPPSORT',4)", source)
        self.assertIn("ISVALID(DRAM0)", source)
        self.assertIn("ISVALID(DRAML0)", source)
        self.assertNotIn("$WTMEAPPSORT.", source)
        self.assertNotIn('"74_BTC/USD$', source)
        self.assertNotIn('"74_US10Y$', source)
        self.assertNotIn("SH518880$", source)
        self.assertIn("'敞口%'", source)
        self.assertIn("IBIT1>-100", source)
        self.assertNotIn("-999", source)
        self.assertIn("IF(RI<=5,0.02,0.52)", source)

    def test_us_mobile_dashboard_matches_working_mobile_structure(self):
        source = (TDX / "WTMEPOOLUSM.txt").read_text(encoding="utf-8")
        source_code = "\n".join(line for line in source.splitlines() if not line.startswith("{"))
        expected = {"DRAM", "FXI", "GLD", "IBIT", "QQQ", "SLV", "SOXX", "SPY", "XLE"}
        for symbol in expected:
            self.assertIn(f'"74_{symbol}$WTMEAPPSORT.SORTV"', source)
            self.assertIn(f'"74_{symbol}$WTMEAPPSORT.DLEV"', source)
        self.assertNotIn("CALCSTOCKINDEX", source_code)
        self.assertNotIn("ISVALID", source_code)
        self.assertIn("IF(RI<=5,0.02,0.52)", source)
        self.assertNotIn("-999", source)

    def test_helper_hardcodes_current_saved_strategy_parameters(self):
        source = (TDX / "WTMEAPPSORT.txt").read_text(encoding="utf-8")
        for setting in (
            "NN0:=13", "HH0:=6", "LB0:=5", "DP0:=5", "VT0:=15",
            "SD0:=10", "LOSS0:=40", "ML0:=5",
        ):
            self.assertIn(setting, source)
        self.assertIn("WTMEV:", source)
        self.assertIn("ELIGV:", source)
        self.assertIn("DLEV:", source)
        self.assertIn("WTME0,-100", source)
        self.assertNotIn("-999", source)
        lines = source.splitlines()
        output_positions = [
            next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
            for name in ("SORTV", "WTMEV", "ELIGV", "DLEV")
        ]
        self.assertEqual(output_positions, sorted(output_positions))


if __name__ == "__main__":
    unittest.main()
