#!/usr/bin/env python3
"""
将指定标的的 60m / 15m K 线拉到本地 CSV（与 kline_scheduler / get_index_kline 同源）。

写入路径：backend/data/kline_60_{code}.csv、kline_15_{code}.csv

注意：新浪接口单次约 2048 根，覆盖区间随周期而异（60m 回溯更长、15m 更短），
不足以覆盖「2023 年起」全历史步进回测时，需另寻多段拼接或别的数据源。

用法：
  cd backend && python3 scripts/pull_symbol_klines.py --symbol 510300
  cd backend && python3 scripts/pull_symbol_klines.py --symbol sh000001 --start 2020-01-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.indicators import get_index_kline, _kline_15_cache_path, _kline_60_cache_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="拉取 60m/15m K 线到本地 data/kline_*.csv")
    p.add_argument("--symbol", required=True, help="如 510300、sh000001")
    p.add_argument("--start", default="2020-01-01", help="请求起始日（过滤用，不突破接口条数上限）")
    p.add_argument("--only-60", action="store_true", help="只拉 60m")
    p.add_argument("--only-15", action="store_true", help="只拉 15m")
    args = p.parse_args()

    sym = args.symbol.strip()
    if args.only_60 and args.only_15:
        p.error("不能同时指定 --only-60 与 --only-15")

    periods: list[str]
    if args.only_60:
        periods = ["60"]
    elif args.only_15:
        periods = ["15"]
    else:
        periods = ["60", "15"]

    for period in periods:
        result = get_index_kline(
            symbol=sym,
            start_date=args.start,
            end_date=None,
            period=period,
            refresh=True,
        )
        n_api = len(result.get("data", []))
        path = _kline_60_cache_path(sym) if period == "60" else _kline_15_cache_path(sym)
        rows_disk = 0
        if path.is_file():
            try:
                rows_disk = sum(1 for _ in open(path, "rb")) - 1
            except OSError:
                rows_disk = -1
        logging.info(
            "完成 %s period=%s API返回data条数=%d（缠论截断后）本地CSV≈%s 行: %s",
            sym,
            period,
            n_api,
            rows_disk if rows_disk >= 0 else "?",
            path,
        )


if __name__ == "__main__":
    main()
