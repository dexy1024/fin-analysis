"""
东方财富（AKShare fund_etf_hist_min_em）分段拉取场内 ETF 15 分钟 K 线，补全本地 CSV。

新浪 CN_MarketData.getKLineData 单次最多约 2048 根，15m 可回溯日历远短于「2024-03 至今」；
本模块按日历窗口分段请求后去重合并，写入 data/kline_15_{code}.csv（与 indicators 缓存格式一致）。

适用范围：场内 ETF 代码（如 510300）。非 ETF 请改用其它接口。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd
from zoneinfo import ZoneInfo

from services.indicators import _save_kline_15_cache
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

TZ_SH = ZoneInfo("Asia/Shanghai")


def _to_naive_sh(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is not None:
        return ts.tz_convert(TZ_SH).tz_localize(None)
    return ts


_EM_COL_MAP = {
    "时间": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
}


def _normalize_em_minute_df(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = raw.rename(columns={k: v for k, v in _EM_COL_MAP.items() if k in raw.columns})
    if "date" not in df.columns:
        first = str(raw.columns[0])
        if first and first not in df.columns:
            df = raw.rename(columns={first: "date"})
        for old, new in _EM_COL_MAP.items():
            if old in df.columns and new != "date":
                df = df.rename(columns={old: new})
    need = ["date", "open", "high", "low", "close", "volume"]
    for c in need:
        if c not in df.columns:
            raise ValueError(f"东财 15m 返回缺少列 {c}，实际列: {list(df.columns)}")
    out = df[need].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out.sort_values("date").reset_index(drop=True)
    return out


def _em_slice(
    symbol_6: str,
    start_naive: pd.Timestamp,
    end_naive: pd.Timestamp,
    *,
    sleep_sec: float,
) -> pd.DataFrame:
    """
    单次请求东财 ETF 分钟线；symbol_6 为六位代码如 510300。
    """
    start_s = start_naive.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end_naive.strftime("%Y-%m-%d %H:%M:%S")
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            raw = ak.fund_etf_hist_min_em(
                symbol=symbol_6,
                period="15",
                start_date=start_s,
                end_date=end_s,
                adjust="",
            )
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            return _normalize_em_minute_df(raw)
        except EXPECTED_BUSINESS_EXCEPTIONS as e:
            last_err = e
            logging.warning(
                "kline_15_backfill_em: 请求失败 %s ~ %s (%s)，第 %d 次重试",
                start_s,
                end_s,
                e,
                attempt + 1,
            )
            time.sleep(max(1.5, sleep_sec * 2))
        except Exception:
            logging.exception("kline_15_backfill_em: 请求未预期异常 %s ~ %s", start_s, end_s)
            raise
    raise RuntimeError(f"东财 15m 拉取失败 {start_s} ~ {end_s}: {last_err}") from last_err


def backfill_etf_15m_em(
    symbol: str,
    start_date: str,
    end_date: Optional[str] = None,
    *,
    chunk_calendar_days: int = 14,
    sleep_sec: float = 0.6,
) -> pd.DataFrame:
    """
    分段拉取 [start_date, end_date]（含）区间内 15m K 线，合并去重后返回 DataFrame。
    end_date 默认当前上海时间。
    """
    code = symbol.strip().replace("sh", "").replace("sz", "")
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持六位标的代码，收到: {symbol!r}")

    start_naive = _to_naive_sh(pd.Timestamp(start_date))

    if end_date:
        s_end = end_date.strip()
        end_naive = _to_naive_sh(pd.Timestamp(s_end))
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s_end):
            end_naive = end_naive.replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        end_naive = pd.Timestamp.now(tz=TZ_SH).tz_localize(None)

    if end_naive < start_naive:
        raise ValueError("end_date 不能早于 start_date")

    parts: list[pd.DataFrame] = []
    chunk_days = max(1, int(chunk_calendar_days))
    cur = start_naive

    while cur <= end_naive:
        nxt = min(cur + pd.Timedelta(days=chunk_days), end_naive)
        logging.info(
            "kline_15_backfill_em: 拉取 %s 15m %s ~ %s",
            code,
            cur,
            nxt,
        )
        chunk_df = _em_slice(code, cur, nxt, sleep_sec=sleep_sec)
        if not chunk_df.empty:
            parts.append(chunk_df)
        cur = nxt + pd.Timedelta(seconds=1)

    if not parts:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    merged = pd.concat(parts, ignore_index=True)
    merged = merged.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    merged = merged[(merged["date"] >= start_naive) & (merged["date"] <= end_naive)]
    return merged.reset_index(drop=True)


def backfill_etf_15m_em_to_csv(
    symbol: str,
    start_date: str,
    end_date: Optional[str] = None,
    *,
    chunk_calendar_days: int = 14,
    sleep_sec: float = 0.6,
) -> Path:
    """拉取并写入 ``backend/data/kline_15_{symbol}.csv``。"""
    df = backfill_etf_15m_em(
        symbol,
        start_date,
        end_date,
        chunk_calendar_days=chunk_calendar_days,
        sleep_sec=sleep_sec,
    )
    if df.empty:
        logging.warning("kline_15_backfill_em: 无数据写入 %s", symbol)
    _save_kline_15_cache(symbol.strip(), df)
    return Path(__file__).resolve().parents[1] / "data" / f"kline_15_{symbol.strip().replace('/', '_')}.csv"
