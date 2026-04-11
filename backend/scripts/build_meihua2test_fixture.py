#!/usr/bin/env python3
"""
将梅花生物 600873 的日线 / 60m 本地 CSV 复制为测试标的 889999（雷达显示名：梅花2test）。

说明：
- 复制后缠论与双防线计算与 600873 完全一致；若源数据已满足四条件，则 889999 的 full_trigger 与源相同。
- `get_index_kline(..., period=60)` 在未指定 end_date 时会用「当前时刻」截断 K 线；若手工在 CSV 末尾追加
  「晚于源最后一根、且时间戳晚于当前时刻」的 K 线，**不会出现在 API 中**，请勿那样 mock。

用法（在 backend 目录）:
  python3 scripts/build_meihua2test_fixture.py

输出:
  - tests/fixtures/meihua2test/a_daily_qfq_889999.csv、kline_60_889999.csv
  - backend/data 下同名文件（供本机雷达 / 前端读取）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIX = ROOT / "tests" / "fixtures" / "meihua2test"


def main() -> None:
    src_daily = DATA / "a_daily_qfq_600873.csv"
    src_60 = DATA / "kline_60_600873.csv"
    if not src_daily.is_file() or not src_60.is_file():
        print("缺少 600873 源文件:", src_daily, src_60, file=sys.stderr)
        sys.exit(1)

    FIX.mkdir(parents=True, exist_ok=True)

    out_fix_d = FIX / "a_daily_qfq_889999.csv"
    out_fix_h = FIX / "kline_60_889999.csv"
    out_data_d = DATA / "a_daily_qfq_889999.csv"
    out_data_h = DATA / "kline_60_889999.csv"

    shutil.copy2(src_daily, out_fix_d)
    shutil.copy2(src_60, out_fix_h)
    shutil.copy2(src_daily, out_data_d)
    shutil.copy2(src_60, out_data_h)

    print("fixtures:", out_fix_d)
    print("fixtures:", out_fix_h)
    print("installed:", out_data_d)
    print("installed:", out_data_h)


if __name__ == "__main__":
    main()
