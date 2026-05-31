#!/usr/bin/env python3
"""
手动更新 上证指数 + watchlist.json + observation.json + observation_hk.json 的 K 线（防新浪限流：慢速 + 多轮补跑）。

- 始终同步 60m / 15m
- 日线：仅北京时间 16:00 及之后执行时才拉取（当日收盘后）

不含 DEFENSE_RADAR 核心列表——由 kline_scheduler 定时调度。

用法:
  cd /Users/yuguoq/Desktop/CursorProject/fin-analysis && python3 update_data.py
  python3 update_data.py --sleep 5          # 更慢
  python3 update_data.py --sleep 12         # 遇 456 限流时
  python3 update_data.py --max-rounds 6     # 多补几轮
  python3 update_data.py --force-daily      # 16 点前也强制拉日线
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
os.chdir(backend_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

scripts_dir = os.path.join(backend_dir, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

# 默认节奏：比 kline_scheduler(1s) 慢，降低新浪 456 概率
DEFAULT_SLEEP_SEC = 8.0
DEFAULT_PERIOD_SLEEP_SEC = 5.0
DEFAULT_MAX_ROUNDS = 5
TZ_SH = ZoneInfo("Asia/Shanghai")
DAILY_SYNC_AFTER_HOUR = 16


def should_sync_daily(*, force: bool = False) -> bool:
    """北京时间 16:00 及之后才拉日线（当日 K 线收盘后）。"""
    if force:
        return True
    return datetime.now(TZ_SH).hour >= DAILY_SYNC_AFTER_HOUR


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    p = argparse.ArgumentParser(description="手动更新上证 + watchlist+observation+observation_hk K 线")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SEC, help="每标的间隔（秒）")
    p.add_argument(
        "--period-sleep",
        type=float,
        default=DEFAULT_PERIOD_SLEEP_SEC,
        help="同一标的各周期间隔（秒），如 daily 与 60m、60m 与 15m",
    )
    p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="未齐时最多补跑轮数")
    p.add_argument("--skip-signals", action="store_true", help="跳过破位/买卖信号重算")
    p.add_argument(
        "--force-daily",
        action="store_true",
        help="忽略 16 点门槛，强制拉日线（默认仅北京时间 16:00 及之后）",
    )
    args = p.parse_args()

    now_sh = datetime.now(TZ_SH)
    sync_daily = should_sync_daily(force=args.force_daily)
    periods = ("daily", "60", "15") if sync_daily else ("60", "15")
    period_label = "+".join(periods)

    print("=" * 50)
    print("上证 + watchlist + observation + observation_hk K 线增量同步（慢速模式）")
    print(f"北京时间: {now_sh.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"周期: {period_label}")
    if sync_daily:
        print("日线: 已启用（当前 >= 16:00 或 --force-daily）")
    else:
        print(f"日线: 跳过（16:00 前不拉，当前 {now_sh.strftime('%H:%M')}）")
    print(f"间隔: 标的={args.sleep}s  周期间={args.period_sleep}s  最多{args.max_rounds}轮")
    print("=" * 50)

    from sync_watchlist_observation import run_watchlist_observation_sync

    print(f"\n[1/3] 增量拉取 {period_label} ...")
    kline_rc = run_watchlist_observation_sync(
        periods=periods,
        sleep_sec=args.sleep,
        period_sleep_sec=args.period_sleep,
        max_rounds=args.max_rounds,
        include_hk=True,
        include_sh_index=True,
        etf_em_fallback=True,
    )

    if not args.skip_signals:
        print("\n[2/3] 重算破位 / 买卖信号 ...")
        from services.buy_sell_signals import compute_and_save_buy_sell_signals
        from services.defense_radar import compute_and_save_broken_symbols

        try:
            broken_path = compute_and_save_broken_symbols()
            print(f"  破位: {broken_path}")
        except Exception as exc:
            logging.exception("破位重算失败: %s", exc)

        try:
            buy_sell_path = compute_and_save_buy_sell_signals()
            print(f"  买卖信号: {buy_sell_path}")
        except Exception as exc:
            logging.exception("买卖信号重算失败: %s", exc)
    else:
        print("\n[2/3] 跳过信号重算")

    print("\n[3/3] 数据检查")
    files = glob.glob("data/kline_60_*.csv")
    for f in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
        mtime = os.path.getmtime(f)
        dt = datetime.fromtimestamp(mtime)
        print(f"  {os.path.basename(f)} - {dt.strftime('%Y-%m-%d %H:%M:%S')}")

    for symbol in ("sh000001", "513130", "510300", "hk01810"):
        filepath = f"data/kline_60_{symbol}.csv"
        if os.path.exists(filepath):
            with open(filepath, encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) >= 2:
                print(f"  60m {symbol} 末行: {lines[-1].strip()}")
        if sync_daily:
            daily_candidates = [
                f"data/index_daily_{symbol}.csv",
                f"data/hk_daily_{symbol}.csv",
                f"data/a_daily_qfq_{symbol}.csv",
                f"data/a_daily_nq_{symbol}.csv",
            ]
            for daily_path in daily_candidates:
                if os.path.exists(daily_path):
                    with open(daily_path, encoding="utf-8") as fh:
                        lines = fh.readlines()
                    if len(lines) >= 2:
                        print(f"  daily {symbol} 末行: {lines[-1].strip()}")
                    break

    print("\n" + "=" * 50)
    if kline_rc == 0:
        print("完成：全部标的已齐")
    else:
        print("完成：部分标的未齐，可加大 --sleep 后重试")
    print("=" * 50)
    return kline_rc


if __name__ == "__main__":
    raise SystemExit(main())
