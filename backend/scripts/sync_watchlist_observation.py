#!/usr/bin/env python3
"""
watchlist.json + observation.json K 线增量补拉（与 HS300 同源逻辑）。

- 扫描：相对今日缺 daily / 60m / 15m 的标的
- 拉取：从本地 CSV 最后一根起增量（incremental_start_date / incremental_daily_start_date）
- 写盘：与远端按 date 合并去重（非整窗重拉）
- 默认跳过 hk 开头标的

用法：
  cd backend && python3 scripts/sync_watchlist_observation.py
  cd backend && python3 scripts/sync_watchlist_observation.py --dry-run
  cd backend && python3 scripts/sync_watchlist_observation.py --sleep 3 --max-rounds 5
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.observation_data import load_watchlist_observation_symbols
from services.index_cache import _is_likely_etf_code
from services.kline_15_backfill_em import backfill_etf_15m_em_to_csv
from services.kline_hs300_incremental_sync import (
    _sync_symbol,
    audit_kline_freshness,
    incremental_start_date,
    run_kline_stale_repair,
)

TZ = ZoneInfo("Asia/Shanghai")


def _pairs(*, no_hk: bool) -> list[tuple[str, str]]:
    return load_watchlist_observation_symbols(include_hk=not no_hk)


def _etf_15m_em_fallback(pairs: list[tuple[str, str]], sleep_sec: float) -> int:
    """ETF 15m 新浪仍失败时，东财分段从增量起点补拉。"""
    import time

    target = datetime.now(TZ).strftime("%Y-%m-%d")
    report = audit_kline_freshness(pairs, target)
    n = 0
    for sf in report.stale:
        if not sf.needs_15 or not _is_likely_etf_code(sf.code):
            continue
        start = incremental_start_date(sf.code, "15")
        if sleep_sec > 0:
            time.sleep(sleep_sec)
        try:
            backfill_etf_15m_em_to_csv(
                sf.code,
                start[:10],
                None,
                chunk_calendar_days=3,
                sleep_sec=max(0.8, sleep_sec * 0.4),
            )
            logging.info("%s ETF 15m 东财增量补拉完成", sf.code)
            n += 1
        except Exception:
            logging.exception("%s ETF 15m 东财补拉失败", sf.code)
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="watchlist+observation 增量 K 线补拉")
    p.add_argument("--dry-run", action="store_true", help="只扫描并打印增量起点，不拉网")
    p.add_argument("--include-hk", action="store_true", help="包含港股（默认跳过 hk 标的）")
    p.add_argument("--sleep", type=float, default=3.0, help="每标的处理后的间隔（秒）")
    p.add_argument("--max-rounds", type=int, default=3, help="未齐时最多补跑轮数")
    p.add_argument("--etf-em-fallback", action="store_true", default=True, help="ETF 15m 失败后试东财")
    args = p.parse_args()

    no_hk = not args.include_hk
    pairs = _pairs(no_hk=no_hk)
    target = datetime.now(TZ).strftime("%Y-%m-%d")

    if args.dry_run:
        report = audit_kline_freshness(pairs, target)
        logging.info(
            "dry-run: 已齐 %d/%d，待补 %d (缺 daily=%d 60m=%d 15m=%d)",
            report.fresh,
            report.total,
            len(report.stale),
            report.need_daily,
            report.need_60,
            report.need_15,
        )
        for sf in report.stale:
            r = _sync_symbol(sf.code, sf.name, sf.periods_to_sync, dry_run=True)
            logging.info(
                "  %s %s 缺 %s | 增量起点 daily=%s 60=%s 15=%s",
                sf.code,
                sf.name or "-",
                sf.missing_labels(),
                r.start_daily or "-",
                r.start_60 or "-",
                r.start_15 or "-",
            )
        return 0

    for round_i in range(1, max(1, args.max_rounds) + 1):
        report, summary = run_kline_stale_repair(
            pairs,
            label="watchlist+observation",
            target_date=target,
            sleep_sec=args.sleep,
            dry_run=False,
        )
        if args.etf_em_fallback and report.need_15 > 0:
            _etf_15m_em_fallback(pairs, args.sleep)

        report = audit_kline_freshness(pairs, target)
        logging.info(
            "第 %d 轮结束: 已齐 %d/%d，仍缺 %d",
            round_i,
            report.fresh,
            report.total,
            len(report.stale),
        )
        if not report.stale:
            logging.info("全部标的已齐")
            return 0

    logging.warning("仍有 %d 个标的未齐，可稍后加大 --sleep 重试", len(report.stale))
    for sf in report.stale[:20]:
        logging.warning(
            "  %s %s 缺 %s | daily=%s 60m=%s 15m=%s",
            sf.code,
            sf.name or "-",
            sf.missing_labels(),
            sf.daily_last or "-",
            sf.m60_last or "-",
            sf.m15_last or "-",
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
