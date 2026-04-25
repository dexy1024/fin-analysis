#!/usr/bin/env python3
"""
批量刷新K线数据，确保日线/60分钟/15分钟均达到258根以上。
用法: cd /Users/yuguoq/Desktop/CursorProject/fin-analysis && python3 backend/scripts/refresh_kline_258.py
"""

import sys
import os
import time
import logging
import pandas as pd
from datetime import datetime

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.indicators import get_index_kline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

DATA_DIR = os.path.join(backend_dir, "data")

# 目标K线数量
TARGET = 258

# 标的列表（来自 observation.json + watchlist.json）
OBS_CODES = [
    "510300", "588000", "512400", "512690", "159985", "159227", "159992",
    "002475", "600938", "000858", "000429", "601225", "601288", "000338",
    "000538", "688981", "688041", "002230", "hk00175",
]
WATCH_CODES = [
    "603317", "600660", "000895", "600276", "000001", "000333", "601728",
    "000651", "002415", "601138", "600900", "000423", "513130", "159915",
    "588200", "159755", "515790", "159899", "513360", "hk01810", "hk06862",
]
ALL_CODES = OBS_CODES + WATCH_CODES


def count_csv_rows(filepath: str) -> int:
    if not os.path.exists(filepath):
        return 0
    try:
        df = pd.read_csv(filepath)
        return len(df)
    except Exception:
        return 0


def get_daily_path(code: str) -> str:
    if code.startswith("hk"):
        return os.path.join(DATA_DIR, f"hk_daily_{code}.csv")
    for prefix in ["a_daily_qfq_", "a_daily_nq_"]:
        path = os.path.join(DATA_DIR, f"{prefix}{code}.csv")
        if os.path.exists(path):
            return path
    return os.path.join(DATA_DIR, f"a_daily_qfq_{code}.csv")


def refresh_symbol(code: str, period: str, start_date: str) -> int:
    """刷新单个标的的K线，返回获取到的K线数量。"""
    try:
        result = get_index_kline(
            symbol=code,
            start_date=start_date,
            end_date=None,
            period=period,
            refresh=True,
        )
        count = len(result.get("data", []))
        logging.info(f"[{code}] {period} 刷新成功: {count} 根")
        return count
    except Exception as e:
        logging.error(f"[{code}] {period} 刷新失败: {e}")
        return -1


def main():
    print("=" * 60)
    print(f"K线数据补齐脚本 - 目标: 各周期 >= {TARGET} 根")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 先统计当前状态
    needs_refresh = {
        "daily": [],
        "60": [],
        "15": [],
    }

    for code in ALL_CODES:
        daily_path = get_daily_path(code)
        k60_path = os.path.join(DATA_DIR, f"kline_60_{code}.csv")
        k15_path = os.path.join(DATA_DIR, f"kline_15_{code}.csv")

        daily_count = count_csv_rows(daily_path)
        k60_count = count_csv_rows(k60_path)
        k15_count = count_csv_rows(k15_path)

        if daily_count < TARGET:
            needs_refresh["daily"].append((code, daily_count))
        if k60_count < TARGET:
            needs_refresh["60"].append((code, k60_count))
        if k15_count < TARGET:
            needs_refresh["15"].append((code, k15_count))

    print(f"\n需要补齐的标的:")
    print(f"  日线: {len(needs_refresh['daily'])} 个 -> {needs_refresh['daily']}")
    print(f"  60分钟: {len(needs_refresh['60'])} 个 -> {[(c,n) for c,n in needs_refresh['60']]}")
    print(f"  15分钟: {len(needs_refresh['15'])} 个 -> {[(c,n) for c,n in needs_refresh['15']]}")

    # 1. 刷新日线
    if needs_refresh["daily"]:
        print(f"\n[1/3] 开始刷新日线...")
        for code, old_count in needs_refresh["daily"]:
            # 日线拉取380天历史（kline_scheduler默认）
            new_count = refresh_symbol(code, "daily", "2025-01-01")
            if new_count > 0 and new_count < TARGET:
                logging.warning(f"[{code}] 日线仅获取 {new_count} 根，可能上市时间不足")
            time.sleep(0.8)

    # 2. 刷新60分钟线
    if needs_refresh["60"]:
        print(f"\n[2/3] 开始刷新60分钟线...")
        for code, old_count in needs_refresh["60"]:
            # A股用新浪接口可获取2048根（约480天），港股可能需要更短
            start = "2025-01-01"
            new_count = refresh_symbol(code, "60", start)
            if new_count > 0 and new_count < TARGET:
                logging.warning(f"[{code}] 60分钟仅获取 {new_count} 根，数据源可能有限制")
            time.sleep(1.2)  # 降低限流风险

    # 3. 刷新15分钟线
    if needs_refresh["15"]:
        print(f"\n[3/3] 开始刷新15分钟线...")
        for code, old_count in needs_refresh["15"]:
            start = "2025-10-01"
            new_count = refresh_symbol(code, "15", start)
            if new_count > 0 and new_count < TARGET:
                logging.warning(f"[{code}] 15分钟仅获取 {new_count} 根，数据源可能有限制")
            time.sleep(1.2)

    # 最终校验
    print(f"\n{'=' * 60}")
    print("最终校验:")
    print(f"{'=' * 60}")
    all_ok = True
    for code in ALL_CODES:
        daily_path = get_daily_path(code)
        k60_path = os.path.join(DATA_DIR, f"kline_60_{code}.csv")
        k15_path = os.path.join(DATA_DIR, f"kline_15_{code}.csv")

        daily_count = count_csv_rows(daily_path)
        k60_count = count_csv_rows(k60_path)
        k15_count = count_csv_rows(k15_path)

        issues = []
        if daily_count < TARGET:
            issues.append(f"日线={daily_count}")
        if k60_count < TARGET:
            issues.append(f"60min={k60_count}")
        if k15_count < TARGET:
            issues.append(f"15min={k15_count}")

        if issues:
            all_ok = False
            print(f"  {code}: {' | '.join(issues)}")
        else:
            print(f"  {code}: ✓ 全部充足 (日线={daily_count}, 60min={k60_count}, 15min={k15_count})")

    if all_ok:
        print("\n✅ 所有标的3周期K线均达到目标数量!")
    else:
        print("\n⚠️ 部分标的存在不足，请检查日志。")


if __name__ == "__main__":
    main()
