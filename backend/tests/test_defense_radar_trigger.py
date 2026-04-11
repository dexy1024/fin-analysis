"""雷达：严格末三 K 底分型与伏击带单元测试。"""
from __future__ import annotations

import unittest

from services.defense_radar import (
    _price_in_tier1_or_ultimate_zone,
    macd_momentum_ok_two_down_pens,
    strict_blue_triangle_last_three_raw,
)


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
