from __future__ import annotations

import math
from pathlib import Path
import runpy
import unittest

from services.indicator_service import calculate_indicator_values, calculate_wtme


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUTUBULL_DIR = PROJECT_ROOT / "other_platform" / "futubull"


class _Sequence(list):
    """富途 Sequence 在这些脚本所需范围内的最小测试替身。"""


def _load_futubull_script(filename: str, rows: list[dict]) -> dict:
    def values(field: str) -> _Sequence:
        return _Sequence(float(row[field]) for row in rows)

    return runpy.run_path(
        str(FUTUBULL_DIR / filename),
        init_globals={
            "indicator": lambda *args, **kwargs: None,
            "close": lambda: values("close"),
            "high": lambda: values("high"),
            "low": lambda: values("low"),
        },
    )


def _sample_rows(count: int = 40) -> list[dict]:
    rows = []
    previous_close = 100.0
    for index in range(count):
        direction = 1 if index % 5 not in {0, 4} else -1
        close = previous_close + direction * (0.35 + (index % 7) * 0.11)
        rows.append({
            "date": f"2026-01-{index + 1:02d}",
            "open": previous_close,
            "high": max(previous_close, close) + 0.2 + (index % 3) * 0.05,
            "low": min(previous_close, close) - 0.15 - (index % 2) * 0.04,
            "close": close,
            "volume": 1,
        })
        previous_close = close
    return rows


class FutubullIndicatorParityTests(unittest.TestCase):
    def assert_series_equal(
        self,
        actual: list[float],
        expected: list[float | None],
    ) -> None:
        self.assertEqual(len(actual), len(expected))
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected)):
            if expected_value is None:
                self.assertTrue(math.isnan(actual_value), msg=f"index={index}")
            else:
                self.assertAlmostEqual(actual_value, expected_value, places=12)

    def test_relative_atr_matches_canonical_project_formula(self) -> None:
        rows = _sample_rows()
        module = _load_futubull_script("relative_atr.py", rows)

        for period in (1, 3, 13):
            with self.subTest(period=period):
                self.assert_series_equal(
                    list(module["relative_atr"](period)),
                    calculate_indicator_values(rows, "RATR", period),
                )

    def test_wtme_matches_canonical_project_formula(self) -> None:
        rows = _sample_rows()
        module = _load_futubull_script("wtme.py", rows)

        for period, half_life in ((2, 0.5), (3, 2.0), (13, 6.0)):
            with self.subTest(period=period, half_life=half_life):
                self.assert_series_equal(
                    list(module["wtme"](period, half_life)),
                    calculate_wtme(rows, period, half_life),
                )


if __name__ == "__main__":
    unittest.main()
