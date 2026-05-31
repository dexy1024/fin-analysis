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
  cd backend && python3 scripts/sync_watchlist_observation.py --periods 60,15
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
from services.kline_hs300_incremental_sync import (
    Period,
    _sync_symbol,
    audit_kline_freshness_for_periods,
    run_pairs_kline_sync_stable,
)

TZ = ZoneInfo("Asia/Shanghai")
ALL_PERIODS: tuple[Period, ...] = ("daily", "60", "15")
SH_INDEX: tuple[str, str] = ("sh000001", "上证指数")


def _pairs(*, no_hk: bool, include_sh_index: bool = False) -> list[tuple[str, str]]:
    pairs = load_watchlist_observation_symbols(include_hk=not no_hk)
    if include_sh_index:
        rest = [(c, n) for c, n in pairs if c != SH_INDEX[0]]
        pairs = [SH_INDEX, *rest]
    return pairs


def _parse_periods(raw: str) -> tuple[Period, ...]:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    valid: list[Period] = []
    for p in parts:
        if p in ("daily", "d"):
            valid.append("daily")
        elif p in ("60", "60m"):
            valid.append("60")
        elif p in ("15", "15m"):
            valid.append("15")
        else:
            raise ValueError(f"未知周期: {p!r}，可用 daily/60/15")
    if not valid:
        raise ValueError("至少指定一个周期")
    return tuple(dict.fromkeys(valid))


def run_watchlist_observation_sync(
    *,
    periods: tuple[Period, ...] = ALL_PERIODS,
    sleep_sec: float = 3.0,
    period_sleep_sec: float = 0.0,
    max_rounds: int = 3,
    include_hk: bool = False,
    include_sh_index: bool = False,
    etf_em_fallback: bool = True,
    dry_run: bool = False,
) -> int:
    """
    watchlist + observation 增量 K 线同步（默认跳过港股、放慢节奏防新浪限流）。

    返回 0 表示全部已齐，1 表示仍有未齐标的。
    """
    pairs = _pairs(no_hk=not include_hk, include_sh_index=include_sh_index)
    target = datetime.now(TZ).strftime("%Y-%m-%d")
    period_label = ",".join(periods)

    logging.info(
        "watchlist+observation 同步: 标的=%d 周期=%s sleep=%.1fs 周期间隔=%.1fs 最多%d轮 hk=%s sh=%s",
        len(pairs),
        period_label,
        sleep_sec,
        period_sleep_sec,
        max_rounds,
        include_hk,
        include_sh_index,
    )

    if dry_run:
        report = audit_kline_freshness_for_periods(pairs, periods, target)
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
            sync_periods = tuple(p for p in sf.periods_to_sync if p in periods)
            r = _sync_symbol(sf.code, sf.name, sync_periods, dry_run=True)
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

    report = run_pairs_kline_sync_stable(
        pairs,
        periods=periods,
        sleep_sec=sleep_sec,
        period_sleep_sec=period_sleep_sec,
        max_rounds=max_rounds,
        etf_em_fallback=etf_em_fallback,
        label="watchlist+observation",
    )
    return 0 if not report.stale else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="watchlist+observation 增量 K 线补拉")
    p.add_argument("--dry-run", action="store_true", help="只扫描并打印增量起点，不拉网")
    p.add_argument("--include-hk", action="store_true", help="包含港股（默认跳过 hk 标的）")
    p.add_argument(
        "--periods",
        default="daily,60,15",
        help="同步周期，逗号分隔，如 60,15（默认 daily,60,15）",
    )
    p.add_argument("--sleep", type=float, default=3.0, help="每标的处理后的间隔（秒）")
    p.add_argument(
        "--period-sleep",
        type=float,
        default=0.0,
        help="同一标的各周期间隔（秒），如 60m 与 15m 之间",
    )
    p.add_argument("--max-rounds", type=int, default=3, help="未齐时最多补跑轮数")
    p.add_argument("--etf-em-fallback", action="store_true", default=True, help="ETF 15m 失败后试东财")
    args = p.parse_args()

    try:
        periods = _parse_periods(args.periods)
    except ValueError as exc:
        logging.error("%s", exc)
        return 2

    return run_watchlist_observation_sync(
        periods=periods,
        sleep_sec=args.sleep,
        period_sleep_sec=args.period_sleep,
        max_rounds=args.max_rounds,
        include_hk=args.include_hk,
        etf_em_fallback=args.etf_em_fallback,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
