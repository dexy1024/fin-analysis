#!/usr/bin/env python3
"""
HS300（watchlist_hs300.json）K 线增量同步：daily + 60m + 15m，未齐自动补跑重试。

用法（仓库根目录）:
  ./sync_hs300_klines.sh                    # 全量 + 未齐补跑重试，sleep 1s
  ./sync_hs300_klines.sh --dry-run          # 只扫描待补列表
  ./sync_hs300_klines.sh --stale-only       # 跳过全量，只补未齐并重试
  PYTHONPATH=backend python3 backend/scripts/sync_hs300_kline_incremental.py --codes 603260
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.kline_hs300_incremental_sync import (  # noqa: E402
    Period,
    run_hs300_kline_incremental,
    run_hs300_kline_sync_until_fresh,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HS300 K 线同步（daily+60m+15m），支持未齐自动补跑重试",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="含日线（--until-fresh 模式下默认 daily+60m+15m；单独用时须显式指定）",
    )
    parser.add_argument(
        "--until-fresh",
        action="store_true",
        help="全量同步后扫描，未齐则仅补缺失周期并重试直至全部最新（sh 脚本默认开启）",
    )
    parser.add_argument(
        "--stale-only",
        action="store_true",
        help="跳过首轮全量，直接补跑未齐标的并重试",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=10,
        help="--until-fresh 最大补跑轮数（默认 10）",
    )
    parser.add_argument(
        "--target-date",
        type=str,
        default=None,
        help="目标交易日 YYYY-MM-DD，默认北京时间今日",
    )
    parser.add_argument("--dry-run", action="store_true", help="只扫描待补列表，不拉网")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个标的（调试）")
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="每标的处理后的休眠秒数（默认 1.0；传 0 可关闭）",
    )
    parser.add_argument(
        "--codes",
        type=str,
        default=None,
        help="逗号分隔 code 列表",
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--60-only", action="store_true", dest="only_60", help="只同步 60m（不含 until-fresh）")
    g.add_argument("--15-only", action="store_true", dest="only_15", help="只同步 15m（不含 until-fresh）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None

    if args.until_fresh or args.stale_only:
        report, rounds = run_hs300_kline_sync_until_fresh(
            target_date=args.target_date,
            sleep_sec=args.sleep,
            max_rounds=args.max_rounds,
            stale_only=args.stale_only,
            dry_run=args.dry_run,
            limit=args.limit,
            codes=codes,
        )
        if args.dry_run:
            return 0
        return 0 if not report.stale else 1

    if args.only_60:
        periods: tuple[Period, ...] = ("daily", "60") if args.daily else ("60",)
    elif args.only_15:
        periods = ("daily", "15") if args.daily else ("15",)
    elif args.daily:
        periods = ("daily", "60", "15")
    else:
        periods = ("60", "15")

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
