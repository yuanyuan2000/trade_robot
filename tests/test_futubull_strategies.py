import ast
import math
import unittest
from pathlib import Path

from services.backtest.code_strategies import RapidDropAtrRotationStrategy
from services.backtest.code_strategies import SevenStarEtfRotationStrategy
from services.indicator_service import calculate_wtme_components


ROOT = Path(__file__).resolve().parents[1]
FUTUBULL_DIR = ROOT / "other_platform" / "futubull"
STRATEGY_FILES = (
    "rapid_drop_ratr_rotation.py",
    "rapid_drop_wtme_rotation.py",
    "sevenstar_etf_rotation.py",
)


class _StrategyBase:
    pass


def _load_strategy(filename):
    namespace = {"StrategyBase": _StrategyBase, "math_log": math.log, "power": pow}
    source = (FUTUBULL_DIR / filename).read_text(encoding="utf-8")
    exec(compile(source, filename, "exec"), namespace)
    return namespace["Strategy"], source


class FutubullStrategySourceTests(unittest.TestCase):
    def test_all_strategy_sources_parse_and_use_conservative_editor_syntax(self):
        forbidden = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        for filename in STRATEGY_FILES:
            with self.subTest(filename=filename):
                source = (FUTUBULL_DIR / filename).read_text(encoding="utf-8")
                tree = ast.parse(source)
                self.assertFalse(any(isinstance(node, forbidden) for node in ast.walk(tree)))
                self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body))
                self.assertFalse(any(isinstance(node, ast.Expr) for node in tree.body))
                for node in ast.walk(tree):
                    if isinstance(node, ast.BoolOp):
                        self.assertEqual(node.lineno, node.end_lineno)

    def test_all_strategies_declare_security_type_and_completed_signal_bar(self):
        for filename in STRATEGY_FILES:
            with self.subTest(filename=filename):
                source = (FUTUBULL_DIR / filename).read_text(encoding="utf-8")
                self.assertIn("declare_strategy_type(AlgoStrategyType.SECURITY)", source)
                self.assertIn("bar_type=BarType.M1, select=2", source)
                self.assertNotIn("TimeZone.MARKET_TIME_ZONE", source)

    def test_data_prepare_hints_cover_every_declared_trigger_symbol(self):
        expected_counts = {
            "rapid_drop_ratr_rotation.py": 5,
            "rapid_drop_wtme_rotation.py": 7,
            "sevenstar_etf_rotation.py": 8,
        }
        for filename, expected in expected_counts.items():
            with self.subTest(filename=filename):
                source = (FUTUBULL_DIR / filename).read_text(encoding="utf-8")
                tree = ast.parse(source)
                strategy = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Strategy")
                hints = next(node for node in strategy.body if isinstance(node, ast.FunctionDef) and node.name == "_data_prepare_hints")
                subjects = set()
                for node in ast.walk(hints):
                    if not isinstance(node, ast.Call):
                        continue
                    if not isinstance(node.func, ast.Name) or node.func.id != "bar_close":
                        continue
                    for keyword in node.keywords:
                        if keyword.arg != "symbol" or not isinstance(keyword.value, ast.Attribute):
                            continue
                        subjects.add(keyword.value.attr)
                self.assertEqual(len(subjects), expected)


class FutubullStrategyFormulaTests(unittest.TestCase):
    def test_ratr_atr_modes_match_project_strategy(self):
        strategy_type, _source = _load_strategy("rapid_drop_ratr_rotation.py")
        strategy = strategy_type()
        rows = []
        close = 100.0
        for index in range(40):
            close += (index % 5) - 1.5
            rows.append([close + 2.0 + index * 0.01, close - 1.0, close])
        project_rows = []
        for index, row in enumerate(rows):
            project_rows.append({"date": str(index), "high": row[0], "low": row[1], "close": row[2]})
        mode_names = {1: "wilder", 2: "ema", 3: "linear", 4: "simple"}
        for mode, name in mode_names.items():
            with self.subTest(mode=name):
                actual = strategy._atr_series(rows, 5, mode)
                expected = RapidDropAtrRotationStrategy._atr_series(project_rows, 5, name)
                for actual_value, expected_value in zip(actual, expected):
                    if expected_value is None:
                        self.assertIsNone(actual_value)
                    else:
                        self.assertAlmostEqual(actual_value, expected_value, places=12)

    def test_wtme_partial_observation_matches_manual_formula(self):
        strategy_type, _source = _load_strategy("rapid_drop_wtme_rotation.py")
        strategy = strategy_type()
        rows = [
            [101.0, 99.0, 100.0],
            [103.0, 100.0, 102.0],
            [104.0, 101.0, 103.0],
            [106.0, 102.0, 105.0],
        ]
        current = 104.0
        period = 4
        half_life = 2.0
        epsilon = 1e-8
        raw_weights = []
        for index in range(period):
            raw_weights.append(2.0 ** (-(period - 1 - index) / half_life))
        weight_total = sum(raw_weights)
        returns = [0.02, 1.0 / 102.0, 2.0 / 103.0, -1.0 / 105.0]
        true_ranges = [3.0 / 100.0, 3.0 / 102.0, 4.0 / 103.0, 1.0 / 105.0]
        weighted_return = 0.0
        weighted_range = 0.0
        for index in range(period):
            weight = raw_weights[index] / weight_total
            weighted_return += weight * returns[index]
            weighted_range += weight * true_ranges[index]
        expected = 100.0 * weighted_return / (weighted_range + epsilon)
        actual = strategy._wtme_score(rows, current, period, half_life, epsilon)
        self.assertAlmostEqual(actual, expected, places=12)
        project_rows = []
        for index, row in enumerate(rows):
            project_rows.append({"date": str(index), "high": row[0], "low": row[1], "close": row[2]})
        previous_close = rows[-1][2]
        project_rows.append({"date": "current", "high": max(previous_close, current), "low": min(previous_close, current), "close": current})
        components = calculate_wtme_components(project_rows, period, half_life, epsilon)
        self.assertAlmostEqual(actual, components["value"], places=12)

    def test_sevenstar_consistent_and_legacy_formulas_match_project(self):
        strategy_type, _source = _load_strategy("sevenstar_etf_rotation.py")
        strategy = strategy_type()
        prices = [100.0 + index * 0.7 + math.sin(index / 3.0) for index in range(26)]
        actual_consistent = strategy._weighted_trend(prices, 1)
        expected_consistent = SevenStarEtfRotationStrategy._weighted_trend(prices, 25)
        actual_legacy = strategy._weighted_trend(prices, 2)
        expected_legacy = SevenStarEtfRotationStrategy._legacy_weighted_trend(prices, 25)
        for actual, expected in zip(actual_consistent, expected_consistent):
            self.assertAlmostEqual(actual, expected, places=11)
        for actual, expected in zip(actual_legacy, expected_legacy):
            self.assertAlmostEqual(actual, expected, places=11)


if __name__ == "__main__":
    unittest.main()
