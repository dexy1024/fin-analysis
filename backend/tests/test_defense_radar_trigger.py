"""雷达：严格末三 K 底分型与伏击带单元测试。"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from services.defense_radar import (
    _price_in_tier1_or_ultimate_zone,
    macd_momentum_ok_two_down_pens,
    analyze_symbol,
    strict_blue_triangle_last_three_raw,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIR = _BACKEND_ROOT / "tests" / "fixtures" / "meihua2test"
_DATA_DIR = _BACKEND_ROOT / "data"


class TestStrictBlueTriangle(unittest.TestCase):
    def test_empty_false(self) -> None:
        self.assertFalse(strict_blue_triangle_last_three_raw([]))

    def test_strict_bottom_and_k3_confirm(self) -> None:
        bars = [
            {"date": "2026-01-01", "open": 10, "high": 10, "low": 9, "close": 9.5, "volume": 1},
            {"date": "2026-01-02", "open": 9.5, "high": 9.5, "low": 8, "close": 8.2, "volume": 1},
            {"date": "2026-01-03", "open": 8.2, "high": 10, "low": 8.4, "close": 9.2, "volume": 1},
        ]
        self.assertTrue(strict_blue_triangle_last_three_raw(bars))

    def test_k3_close_not_above_k2_low(self) -> None:
        bars = [
            {"date": "a", "open": 10, "high": 10, "low": 9, "close": 9.5, "volume": 1},
            {"date": "b", "open": 9.5, "high": 9.5, "low": 8, "close": 8.2, "volume": 1},
            {"date": "c", "open": 8.2, "high": 9, "low": 8.1, "close": 7.9, "volume": 1},
        ]
        self.assertFalse(strict_blue_triangle_last_three_raw(bars))

    def test_equal_high_not_strict(self) -> None:
        bars = [
            {"date": "2026-02-01", "open": 10, "high": 10, "low": 9, "close": 9.5, "volume": 1},
            {"date": "2026-02-02", "open": 9, "high": 10, "low": 8, "close": 8.5, "volume": 1},
            {"date": "2026-02-03", "open": 8.5, "high": 10, "low": 8.2, "close": 9, "volume": 1},
        ]
        self.assertFalse(strict_blue_triangle_last_three_raw(bars))


class TestDefenseZone(unittest.TestCase):
    def test_in_first_band(self) -> None:
        self.assertTrue(_price_in_tier1_or_ultimate_zone(100.0, 100.0, 100.0))

    def test_in_ultimate_band_when_distinct(self) -> None:
        c_zd, a_zd = 10.0, 5.0
        p = 5.0
        self.assertTrue(_price_in_tier1_or_ultimate_zone(p, c_zd, a_zd))

    def test_watch_gap_false(self) -> None:
        c_zd, a_zd = 10.0, 5.0
        p = 7.0
        self.assertFalse(_price_in_tier1_or_ultimate_zone(p, c_zd, a_zd))

    def test_far_below_false(self) -> None:
        c_zd, a_zd = 10.0, 5.0
        p = 1.0
        self.assertFalse(_price_in_tier1_or_ultimate_zone(p, c_zd, a_zd))


class TestMeihua2testFixture(unittest.TestCase):
    """梅花2test（889999）与 600873 数据一致时，四条件扳机应一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._ready = False
        if not (_DATA_DIR / "kline_60_600873.csv").is_file():
            return
        for name in ("a_daily_qfq_889999.csv", "kline_60_889999.csv"):
            src = _FIXTURE_DIR / name
            if not src.is_file():
                return
            shutil.copy2(src, _DATA_DIR / name)
        cls._ready = True

    def test_meihua2test_matches_600873_triggers(self) -> None:
        if not self._ready:
            self.skipTest("缺少 600873 源数据或未生成 tests/fixtures/meihua2test（运行 scripts/build_meihua2test_fixture.py）")
        a = analyze_symbol("600873", "梅花生物", refresh=False)
        b = analyze_symbol("889999", "梅花2test", refresh=False)
        self.assertEqual(a.full_trigger, b.full_trigger)
        self.assertEqual(a.radar_zone_ok, b.radar_zone_ok)
        self.assertEqual(a.pen_60m_down, b.pen_60m_down)
        self.assertEqual(a.blue_triangle_strict, b.blue_triangle_strict)


class TestMacdMomentum(unittest.TestCase):
    def test_two_down_pens_smaller_area(self) -> None:
        h60 = {
            "pens_effective": [
                {"direction": "up", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                {
                    "direction": "down",
                    "start_date": "2026-01-02",
                    "end_date": "2026-01-03",
                },
                {
                    "direction": "down",
                    "start_date": "2026-01-03",
                    "end_date": "2026-01-04",
                },
            ],
            "data": [
                {"date": "2026-01-02", "macd": {"macd": -10.0}},
                {"date": "2026-01-03", "macd": {"macd": -10.0}},
                {"date": "2026-01-04", "macd": {"macd": -1.0}},
            ],
        }
        ok = macd_momentum_ok_two_down_pens(h60)
        self.assertIsNotNone(ok)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
