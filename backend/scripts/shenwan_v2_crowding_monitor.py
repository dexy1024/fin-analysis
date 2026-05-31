#!/usr/bin/env python3
"""
申万二级行业 · 模块三：拥挤度监控（Amount Share 120 日滚动分位打标）。

基于 shenwan_v2_sector_codes.json，拉取各行业日 K 成交额，计算：
  - 成交额占比 Amount Share = 行业当日成交额 / 全部申万二级行业成交额之和
  - 占比 120 日滚动百分位：当前占比在过去 120 个交易日窗口内的分位（0~100）

状态（优先级：拥挤 > 异动 > 冷清 > 正常）：
  - 拥挤：分位数 > 93%（极度拥挤，警惕见顶）
  - 异动：过去 10 日分位数曾 < 25%，且连续 3 日放量，今日分位数一举突破 55%
  - 冷清：分位数 < 15%（冷清地量，适合左侧关注）
  - 正常：其余

输出 05_shenwan_v2_crowding_monitor.csv（当日全行业快照，按状态优先级 + 分位数排序）。

用法：
    python3 backend/scripts/shenwan_v2_crowding_monitor.py
    python3 backend/scripts/shenwan_v2_crowding_monitor.py -o . --workers 1
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS  # noqa: E402

from shenwan_v2_sector_analysis import (  # noqa: E402
    _sector_code,
    _sector_name,
    build_sw_index_code_map,
    fetch_benchmark_kline,
    load_sectors_json,
    resolve_sw_index_code,
    warn_if_sector_data_lags,
)
from shenwan_v2_volume_price_signals import fetch_sector_ohlc_amount  # noqa: E402

RESULT_CSV = "05_shenwan_v2_crowding_monitor.csv"

ROLLING_WINDOW = 120
PAST_LOW_LOOKBACK = 10
CONSECUTIVE_VOL_DAYS = 3
PCT_CROWDED = 93.0
PCT_COLD = 15.0
PCT_EARLY_LOW = 25.0
PCT_EARLY_BREAK = 55.0

STATUS_CROWDED = "拥挤"
STATUS_EARLY = "异动"
STATUS_COLD = "冷清"
STATUS_NORMAL = "正常"
STATUS_ORDER = {STATUS_CROWDED: 0, STATUS_EARLY: 1, STATUS_NORMAL: 2, STATUS_COLD: 3}

RESULT_COLUMNS = (
    "数据日期",
    "拥挤度状态",
    "行业代码",
    "行业名称",
    "成交额占比_pct",
    "占比120日分位数",
    "占比3日变化_pct",
    "分位数3日斜率",
    "连续3日放量",
    "今日成交额",
)


def _rolling_percentile(series: pd.Series, window: int = ROLLING_WINDOW) -> pd.Series:
    """当前值在 rolling window 内的百分位排名（0~100）。"""

    def _pct(window_vals: pd.Series) -> float:
        if len(window_vals) < window:
            return float("nan")
        current = float(window_vals.iloc[-1])
        return float((window_vals <= current).mean() * 100.0)

    return series.rolling(window, min_periods=window).apply(_pct, raw=False)


def _consecutive_volume_up(amount: pd.Series, days: int = CONSECUTIVE_VOL_DAYS) -> bool:
    if len(amount) < days:
        return False
    tail = amount.iloc[-days:].astype(float)
    return bool((tail.diff().dropna() > 0).all())


def _classify_status(
    pct_series: pd.Series,
    amount: pd.Series,
) -> tuple[str, int]:
    if len(pct_series) < ROLLING_WINDOW or pd.isna(pct_series.iloc[-1]):
        return STATUS_NORMAL, 0

    if float(amount.iloc[-1]) <= 0:
        return STATUS_NORMAL, 0

    today_pct = float(pct_series.iloc[-1])
    yesterday_pct = float(pct_series.iloc[-2]) if len(pct_series) >= 2 else float("nan")
    vol_up = 1 if _consecutive_volume_up(amount) else 0

    if today_pct > PCT_CROWDED:
        return STATUS_CROWDED, vol_up

    past_slice = pct_series.iloc[-(PAST_LOW_LOOKBACK + 1) : -1]
    had_low = bool(len(past_slice) >= PAST_LOW_LOOKBACK and past_slice.min() < PCT_EARLY_LOW)
    breakthrough = (
        not pd.isna(yesterday_pct)
        and today_pct > PCT_EARLY_BREAK
        and yesterday_pct <= PCT_EARLY_BREAK
    )
    if had_low and vol_up and breakthrough:
        return STATUS_EARLY, vol_up

    if today_pct < PCT_COLD:
        return STATUS_COLD, vol_up

    return STATUS_NORMAL, vol_up


def _fetch_sector_amount(
    sector: dict[str, Any],
    code_map: dict[str, str],
) -> tuple[str, str, pd.DataFrame] | None:
    code = _sector_code(sector)
    name = _sector_name(sector)
    sw_index_code = resolve_sw_index_code(name, code_map)
    if not sw_index_code:
        logging.warning("跳过 %s (%s)：无法解析 801 指数代码", name, code)
        return None
    try:
        df = fetch_sector_ohlc_amount(sw_index_code)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("跳过 %s (%s)：%s", name, code, exc)
        return None
    except Exception:
        logging.exception("crowding_monitor: %s (%s) 未预期异常", name, code)
        raise
    return code, name, df[["date", "amount"]].copy()


def run_crowding_monitor(output_dir: Path, *, workers: int = 1) -> Path:
    sectors = load_sectors_json(output_dir)
    code_map = build_sw_index_code_map()
    total = len(sectors)
    workers = max(1, min(workers, total))

    fetched: list[tuple[str, str, pd.DataFrame]] = []
    if workers == 1:
        logging.info("顺序拉取 %d 个行业成交额…", total)
        for idx, sector in enumerate(sectors, start=1):
            if idx % 10 == 0 or idx == total:
                logging.info("进度 %d / %d — %s", idx, total, _sector_name(sector))
            row = _fetch_sector_amount(sector, code_map)
            if row:
                fetched.append(row)
            time.sleep(0.15)
    else:
        logging.info("并发拉取 %d 个行业（workers=%d）…", total, workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_sector_amount, s, code_map): s for s in sectors}
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 25 == 0 or done == total:
                    logging.info("进度 %d / %d", done, total)
                row = fut.result()
                if row:
                    fetched.append(row)

    if not fetched:
        raise RuntimeError("无任何行业成交额数据")

    amount_frames: dict[str, pd.Series] = {}
    names: dict[str, str] = {}
    for code, name, df in fetched:
        s = df.set_index("date")["amount"].astype(float)
        amount_frames[code] = s
        names[code] = name

    panel = pd.DataFrame(amount_frames).sort_index()
    panel = panel.fillna(0.0)
    total_amount = panel.sum(axis=1)
    total_amount = total_amount.replace(0, float("nan"))
    share_panel = panel.div(total_amount, axis=0)

    data_date = panel.index[-1].strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []

    for code in share_panel.columns:
        share = share_panel[code].dropna()
        amount = panel[code]
        pct_series = _rolling_percentile(share, ROLLING_WINDOW)
        if pd.isna(pct_series.iloc[-1]):
            continue

        status, vol_up = _classify_status(pct_series, amount)
        today_amount = float(amount.iloc[-1])
        if today_amount <= 0:
            continue

        today_share = float(share.iloc[-1])
        share_3d_ago = float(share.iloc[-4]) if len(share) >= 4 else float("nan")
        share_delta_3d = (today_share - share_3d_ago) * 100.0 if pd.notna(share_3d_ago) else float("nan")
        pct_today = float(pct_series.iloc[-1])
        pct_3d_ago = float(pct_series.iloc[-4]) if len(pct_series) >= 4 else float("nan")
        pct_slope_3d = pct_today - pct_3d_ago if pd.notna(pct_3d_ago) else float("nan")

        rows.append(
            {
                "数据日期": data_date,
                "行业代码": code,
                "行业名称": names[code],
                "成交额占比_pct": round(today_share * 100.0, 4),
                "占比120日分位数": round(pct_today, 2),
                "占比3日变化_pct": round(share_delta_3d, 4) if pd.notna(share_delta_3d) else None,
                "分位数3日斜率": round(pct_slope_3d, 2) if pd.notna(pct_slope_3d) else None,
                "拥挤度状态": status,
                "连续3日放量": vol_up,
                "今日成交额": round(float(amount.iloc[-1]), 2),
            }
        )

    if not rows:
        raise RuntimeError("分位数计算后无有效行业")

    df = pd.DataFrame(rows)
    df["_status_rank"] = df["拥挤度状态"].map(STATUS_ORDER)
    df = df.sort_values(
        ["_status_rank", "成交额占比_pct"],
        ascending=[True, False],
    ).drop(columns=["_status_rank"])
    df = df[list(RESULT_COLUMNS)].reset_index(drop=True)

    benchmark = fetch_benchmark_kline()
    warn_if_sector_data_lags(benchmark, df, label="拥挤度监控")

    csv_path = output_dir / RESULT_CSV
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    for status in (STATUS_EARLY, STATUS_CROWDED, STATUS_COLD):
        n = int((df["拥挤度状态"] == status).sum())
        if n:
            logging.info("  %s: %d 个", status, n)

    logging.info(
        "完成 %d 个行业 → %s（Normal %d 个）",
        len(df),
        csv_path,
        int((df["拥挤度状态"] == STATUS_NORMAL).sum()),
    )
    return csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="申万二级行业拥挤度监控（120日 Amount Share 分位）")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="并发数，默认 1（顺序执行，更稳）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        csv_path = run_crowding_monitor(args.output_dir.resolve(), workers=args.workers)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.error("执行失败：%s", exc)
        sys.exit(1)
    except Exception:
        logging.exception("执行未预期异常")
        raise

    print(f"结果文件：{csv_path}")


if __name__ == "__main__":
    main()
