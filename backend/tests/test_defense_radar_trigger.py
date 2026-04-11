"""雷达：严格末三 K 底分型与伏击带单元测试。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from services.defense_radar import (
    _price_in_tier1_or_ultimate_zone,
    analyze_meihua2test_symbol,
    analyze_symbol,
    chart_tail_bottom_fractal_ok,
    macd_momentum_ok_two_down_pens,
    strict_blue_triangle_last_three_raw,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIR = _BACKEND_ROOT / "tests" / "fixtures" / "meihua2test"
_DATA_DIR = _BACKEND_ROOT / "data"


class TestChartTailBottomFractal(unittest.TestCase):
    def test_no_fractals_false(self) -> None:
        h60 = {
            "data": [
                {"date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-03", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ],
            "fractals": [],
        }
        self.assertFalse(chart_tail_bottom_fractal_ok(h60))

    def test_bottom_date_in_last_three_true(self) -> None:
        h60 = {
            "data": [
                {"date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-03", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ],
            "fractals": [{"type": "bottom", "date": "2026-01-02", "price": 1.0, "bar_index": 1}],
        }
        self.assertTrue(chart_tail_bottom_fractal_ok(h60))

    def test_bottom_only_earlier_false(self) -> None:
        h60 = {
            "data": [
                {"date": "2026-01-10", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"date": "2026-01-12", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ],
            "fractals": [{"type": "bottom", "date": "2026-01-01", "price": 1.0, "bar_index": 0}],
        }
        self.assertFalse(chart_tail_bottom_fractal_ok(h60))


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

    def test_meihua2test_full_trigger_with_future_k_env(self) -> None:
        """夹具含「未来」60m/日线时，须在 MEIHUA2TEST_FUTURE_K=1 下才能算满序列并维持 full_trigger。"""
        if not self._ready:
            self.skipTest("缺少 600873 源数据或未生成 tests/fixtures/meihua2test（运行 scripts/build_meihua2test_fixture.py）")
        with patch.dict(os.environ, {"MEIHUA2TEST_FUTURE_K": "1"}):
            b = analyze_meihua2test_symbol(refresh=False)
        self.assertTrue(
            b.full_trigger,
            msg="889999 夹具应满足四条件；若失败请重跑 scripts/build_meihua2test_fixture.py",
        )


class TestMeihua2testExtendEndTs(unittest.TestCase):
    """_meihua2test_extend_end_ts_if_demo：仅 889999 + 环境变量开启时扩展 end_ts。"""

    def test_env_off_returns_default(self) -> None:
        from services.indicators import _meihua2test_extend_end_ts_if_demo

        default = pd.Timestamp("2025-06-01")
        with patch.dict(os.environ, {"MEIHUA2TEST_FUTURE_K": ""}):
            self.assertEqual(_meihua2test_extend_end_ts_if_demo("889999", "60", default), default)
        self.assertEqual(_meihua2test_extend_end_ts_if_demo("600873", "60", default), default)

    def test_env_on_60m_uses_csv_max(self) -> None:
        from services import indicators as ind

        default = pd.Timestamp("2020-01-01")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "kline_60_889999.csv"
            pd.DataFrame({"date": ["2026-12-31 15:00:00"]}).to_csv(p, index=False)
            with patch.dict(os.environ, {"MEIHUA2TEST_FUTURE_K": "1"}), patch.object(
                ind, "_kline_60_cache_path", lambda _s: p
            ):
                out = ind._meihua2test_extend_end_ts_if_demo("889999", "60", default)
        self.assertEqual(out, pd.Timestamp("2026-12-31 15:00:00"))

    def test_env_on_daily_uses_csv_max_normalized(self) -> None:
        from services import indicators as ind

        default = pd.Timestamp("2020-01-01")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a_daily_qfq_889999.csv"
            pd.DataFrame({"date": ["2026-11-20"]}).to_csv(p, index=False)
            with patch.dict(os.environ, {"MEIHUA2TEST_FUTURE_K": "1"}), patch.object(
                ind, "_a_share_daily_cache_path", lambda _c: p
            ):
                out = ind._meihua2test_extend_end_ts_if_demo("889999", "daily", default)
        self.assertEqual(out.normalize(), pd.Timestamp("2026-11-20").normalize())


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
        self.assertTrue(ok)

    def test_condition3_false_when_no_area_and_no_shortening(self) -> None:
        h60 = {
            "pens_effective": [
                {"direction": "down", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                {"direction": "down", "start_date": "2026-01-02", "end_date": "2026-01-04"},
            ],
            "data": [
                {"date": "2026-01-02", "macd": {"macd": -1.0}},
                {"date": "2026-01-03", "macd": {"macd": -10.0}},
                {"date": "2026-01-04", "macd": {"macd": -20.0}},
            ],
        }
        self.assertFalse(macd_momentum_ok_two_down_pens(h60))


if __name__ == "__main__":
    unittest.main()
