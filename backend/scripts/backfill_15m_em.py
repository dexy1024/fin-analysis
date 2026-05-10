#!/usr/bin/env python3
"""
使用东财（AKShare fund_etf_hist_min_em）分段补拉场内 ETF 15 分钟 K 线到本地。

覆盖新浪「单次约 2048 根」无法支撑的远程历；写入 backend/data/kline_15_{code}.csv。

用法：
  cd backend && python3 scripts/backfill_15m_em.py --symbol 510300 --start 2024-03-25
  cd backend && python3 scripts/backfill_15m_em.py --symbol 510300 --start 2024-03-25 --end 2026-05-10
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.kline_15_backfill_em import backfill_etf_15m_em_to_csv


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="东财分段补拉 ETF 15m 至 data/kline_15_*.csv")
    p.add_argument("--symbol", default="510300", help="六位 ETF 代码")
    p.add_argument("--start", required=True, help="起始日，如 2024-03-25")
    p.add_argument("--end", default="", help="结束时刻，默认现在；可 YYYY-MM-DD 或含时分秒")
    p.add_argument("--chunk-days", type=int, default=14, help="每段日历天数（默认 14）")
    p.add_argument("--sleep", type=float, default=0.6, help="每段请求后的暂停秒数，防爆限")
    args = p.parse_args()

    end = args.end.strip() or None
    path = backfill_etf_15m_em_to_csv(
        args.symbol,
        args.start,
        end,
        chunk_calendar_days=args.chunk_days,
        sleep_sec=args.sleep,
    )
    print(f"已写入: {path}")


if __name__ == "__main__":
    main()
