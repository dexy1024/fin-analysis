#!/usr/bin/env python3
"""
手动增量同步 watchlist_hs300.json 的 60m/15m K 线（读本地 CSV 最后一根时间作为起点）。

用法（仓库根目录）:
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py --daily
  ./sync_hs300_klines.sh
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py --dry-run --limit 5
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py --60-only --sleep 0.05
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py --codes 000001,600519
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.kline_hs300_incremental_sync import Period, run_hs300_kline_incremental  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="HS300 watchlist 分钟 K 线增量同步")
    parser.add_argument(
        "--daily",
        action="store_true",
        help="含日线：合并拉网写 a_daily_*.csv，get_index_kline 起点为本地最后一根交易日（无文件则 380 日冷启动）。sh 脚本默认带上。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印各标的增量起点，不拉网")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个标的（调试）")
    parser.add_argument("--sleep", type=float, default=0.0, help="每标的处理后的休眠秒数，略降频")
    parser.add_argument(
        "--codes",
        type=str,
        default=None,
        help="逗号分隔 code 列表；指定时不再读取 json 顺序（仍用于子集拉取）",
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--60-only", action="store_true", dest="only_60", help="只同步 60m")
    g.add_argument("--15-only", action="store_true", dest="only_15", help="只同步 15m")
    args = parser.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = None

    if args.only_60:
        periods = ("daily", "60") if args.daily else ("60",)
    elif args.only_15:
        periods = ("daily", "15") if args.daily else ("15",)
    elif args.daily:
        periods = ("daily", "60", "15")
    else:
        periods = ("60", "15")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    summary = run_hs300_kline_incremental(
        periods=periods,
        sleep_sec=args.sleep,
        limit=args.limit,
        codes=codes,
        dry_run=args.dry_run,
    )

    logging.info(
        "完成: 标的数=%d dry_run=%s periods=%s ok_daily=%d fail_daily=%d ok_60=%d fail_60=%d ok_15=%d fail_15=%d",
        summary.symbols,
        args.dry_run,
        periods,
        summary.ok_daily,
        summary.fail_daily,
        summary.ok_60,
        summary.fail_60,
        summary.ok_15,
        summary.fail_15,
    )
    if args.dry_run:
        for r in summary.results[:20]:
            logging.info(
                "dry-run %s %s | start_daily=%s | start_60=%s | start_15=%s",
                r.code,
                r.name or "-",
                r.start_daily or "-",
                r.start_60 or "-",
                r.start_15 or "-",
            )
        if len(summary.results) > 20:
            logging.info("... 其余 %d 条省略", len(summary.results) - 20)
    failed = summary.fail_daily + summary.fail_60 + summary.fail_15
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
