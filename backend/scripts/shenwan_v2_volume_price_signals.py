#!/usr/bin/env python3
"""
申万二级行业：量价资金面趋势监控（无缠论指标）。

基于本地 shenwan_v2_sectors.json，拉取申万官方行业指数日 K（含成交额），计算：
  - is_120h：当前收盘价是否为过去 120 个交易日新高
  - volume_surge：近 5 日均成交额 / 近 60 日均成交额 > 1.5
  - ma20_up：20 日均线拐头向上（MA20 > 1 日前 MA20）

输出 CSV 仅保留同时满足上述三项的行业，按成交额暴增倍数降序排列。

用法：
    python3 backend/scripts/shenwan_v2_volume_price_signals.py
    python3 backend/scripts/shenwan_v2_volume_price_signals.py -o . --workers 1
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

import akshare as ak
import pandas as pd

# 与 shenwan_v2_sector_analysis 共用行业 JSON 约定
SECTORS_JSON = "shenwan_v2_sectors.json"
RESULT_CSV = "shenwan_v2_volume_price_signals.csv"

HIGH_WINDOW = 120
VOLUME_SHORT = 5
VOLUME_LONG = 60
VOLUME_SURGE_RATIO = 1.5
RETURN_WINDOW = 20
MA_WINDOW = 20
MIN_BARS = max(HIGH_WINDOW, VOLUME_LONG, MA_WINDOW + 1, RETURN_WINDOW + 1)

# 复用已有脚本中的行业加载与 801 代码映射
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from shenwan_v2_sector_analysis import (  # noqa: E402
    _sector_code,
    _sector_name,
    build_sw_index_code_map,
    load_sectors_json,
    resolve_sw_index_code,
)


def fetch_sector_ohlc_amount(sw_index_code: str) -> pd.DataFrame:
    """拉取申万行业指数日线（收盘 + 成交额）。"""
    raw = ak.index_hist_sw(symbol=sw_index_code, period="day")
    if raw is None or raw.empty:
        raise ValueError(f"申万指数 {sw_index_code} 无日线数据")

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["日期"], errors="coerce"),
            "close": pd.to_numeric(raw["收盘"], errors="coerce"),
            "amount": pd.to_numeric(raw["成交额"], errors="coerce"),
        }
    )
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    df["amount"] = df["amount"].fillna(0.0)
    if len(df) < MIN_BARS:
        raise ValueError(f"申万指数 {sw_index_code} 有效 K 线不足 {MIN_BARS} 根")
    return df


def _pct_return(close: pd.Series, window: int) -> float:
    start = float(close.iloc[-(window + 1)])
    end = float(close.iloc[-1])
    if start == 0:
        raise ValueError("起始收盘价为 0")
    return (end / start - 1.0) * 100.0


def compute_signals(df: pd.DataFrame) -> dict[str, Any]:
    close = df["close"]
    amount = df["amount"]

    current_close = float(close.iloc[-1])
    high_120 = float(close.iloc[-HIGH_WINDOW:].max())
    is_120h = 1 if current_close >= high_120 else 0

    avg_5 = float(amount.iloc[-VOLUME_SHORT:].mean())
    avg_60 = float(amount.iloc[-VOLUME_LONG:].mean())
    if avg_60 <= 0:
        volume_ratio = 0.0
        volume_surge = 0
    else:
        volume_ratio = avg_5 / avg_60
        volume_surge = 1 if volume_ratio > VOLUME_SURGE_RATIO else 0

    ma20 = close.rolling(MA_WINDOW).mean()
    ma20_now = float(ma20.iloc[-1])
    ma20_prev = float(ma20.iloc[-2])
    ma20_up = 1 if (not pd.isna(ma20_now) and not pd.isna(ma20_prev) and ma20_now > ma20_prev) else 0

    return_20 = _pct_return(close, RETURN_WINDOW)

    return {
        "is_120h": is_120h,
        "volume_surge": volume_surge,
        "成交额暴增倍数": round(volume_ratio, 4),
        "近20日涨幅": round(return_20, 4),
        "ma20_up": ma20_up,
        "最新收盘价": round(current_close, 4),
        "数据日期": df["date"].iloc[-1].strftime("%Y-%m-%d"),
    }


def analyze_sector(
    sector: dict[str, Any],
    code_map: dict[str, str],
) -> dict[str, Any] | None:
    code = _sector_code(sector)
    name = _sector_name(sector)
    sw_index_code = resolve_sw_index_code(name, code_map)
    if not sw_index_code:
        logging.warning("跳过 %s (%s)：无法解析 801 指数代码", name, code)
        return None

    try:
        df = fetch_sector_ohlc_amount(sw_index_code)
        signals = compute_signals(df)
    except Exception as exc:  # noqa: BLE001
        logging.warning("跳过 %s (%s)：%s", name, code, exc)
        return None

    return {
        "行业代码": code,
        "行业名称": name,
        **signals,
    }


def run_analysis(output_dir: Path, *, workers: int = 1) -> Path:
    sectors = load_sectors_json(output_dir)
    code_map = build_sw_index_code_map()

    results: list[dict[str, Any]] = []
    total = len(sectors)
    workers = max(1, min(workers, total))

    if workers == 1:
        logging.info("顺序分析 %d 个行业…", total)
        for idx, sector in enumerate(sectors, start=1):
            if idx % 10 == 0 or idx == total:
                logging.info("进度 %d / %d — %s", idx, total, _sector_name(sector))
            row = analyze_sector(sector, code_map)
            if row:
                results.append(row)
            time.sleep(0.15)
    else:
        logging.info("并发分析 %d 个行业（workers=%d）…", total, workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(analyze_sector, s, code_map): s for s in sectors}
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 25 == 0 or done == total:
                    logging.info("进度 %d / %d", done, total)
                row = fut.result()
                if row:
                    results.append(row)

    if not results:
        raise RuntimeError("无任何行业分析结果")

    df = pd.DataFrame(results)
    filtered = df[
        (df["is_120h"] == 1) & (df["volume_surge"] == 1) & (df["ma20_up"] == 1)
    ].sort_values("成交额暴增倍数", ascending=False).reset_index(drop=True)

    csv_path = output_dir / RESULT_CSV
    filtered.to_csv(csv_path, index=False, encoding="utf-8-sig")

    logging.info(
        "完成 %d / %d 个行业，满足三项条件 %d 个 → %s",
        len(df),
        total,
        len(filtered),
        csv_path,
    )
    return csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="申万二级行业量价资金面趋势监控")
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
    output_dir = args.output_dir.resolve()

    try:
        csv_path = run_analysis(output_dir, workers=args.workers)
    except Exception as exc:  # noqa: BLE001
        logging.error("执行失败：%s", exc)
        sys.exit(1)

    print(f"结果文件：{csv_path}")


if __name__ == "__main__":
    main()
