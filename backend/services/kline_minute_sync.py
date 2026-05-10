"""
60m / 15m K 线：仅从数据源拉取并写入 backend/data/kline_{60|15}_*.csv。

与 get_index_kline(period=60|15) 解耦——后者只读本地 CSV 并计算指标/缠论。
调度器、kline_scheduler、手动脚本及 API ?refresh=true 时应调用本模块，而非在消费侧拉网。
"""

from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from services.indicators import (
    _fetch_15m_from_sina,
    _fetch_60m_from_sina,
    _fetch_hk_60m_from_akshare,
    _fetch_hk_60m_from_yfinance,
    _fetch_hk_min_from_akshare,
    _fetch_hk_min_from_yfinance,
    _meihua2test_extend_end_ts_if_demo,
    _refresh_daily_cache_for_kline_symbol,
    _save_kline_15_cache,
    _save_kline_60_cache,
    _split_kline_symbol,
    _to_sina_symbol,
)

PeriodMinute = Literal["60", "15"]


def _minute_range_ts(symbol: str, start_date: str, end_date: str | None, period: PeriodMinute) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = pd.to_datetime(start_date)
    if end_date:
        end_ts = pd.to_datetime(end_date)
        if end_ts.normalize() == end_ts:
            end_ts = end_ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    else:
        end_ts = pd.Timestamp.now()
    end_ts = _meihua2test_extend_end_ts_if_demo(symbol, period, end_ts)
    if end_ts < start_ts:
        raise ValueError("end_date 不能早于 start_date")
    return start_ts, end_ts


def _normalize_ohlcv_df(df: pd.DataFrame, period: str) -> pd.DataFrame:
    rename_map = {
        "时间": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
    }
    df = df.rename(columns=rename_map)
    req = {"date", "open", "high", "low", "close", "volume"}
    if not req.issubset(df.columns):
        raise ValueError(f"{period} 行情数据缺少必要字段，实际: {list(df.columns)}")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def sync_minute_kline_to_csv(
    symbol: str,
    period: PeriodMinute,
    start_date: str,
    end_date: str | None = None,
) -> int:
    """
    拉取 [start_date, end_date]（end 默认当前时刻）内分钟 K 线，写入对应 kline_*.csv。
    返回写入行数。
    """
    sym = symbol.strip()
    start_ts, end_ts = _minute_range_ts(sym, start_date, end_date, period)
    api_sym, src = _split_kline_symbol(sym)

    try:
        _refresh_daily_cache_for_kline_symbol(sym)
    except Exception:
        logging.exception("kline_minute_sync: 顺带刷新日线失败 %s", sym)

    if period == "60":
        if src in ("a_share", "index"):
            sina_sym = _to_sina_symbol(sym, src, api_sym)
            raw = _fetch_60m_from_sina(sina_sym, start_ts, end_ts)
        elif src == "hk":
            try:
                raw = _fetch_hk_60m_from_akshare(api_sym, start_ts, end_ts)
            except Exception:
                logging.exception("kline_minute_sync: 港股 60m AKShare 失败，回退 yfinance %s", api_sym)
                raw = _fetch_hk_60m_from_yfinance(api_sym, start_ts, end_ts)
        else:
            raise ValueError(f"不支持标的: {sym}")
        df = _normalize_ohlcv_df(raw, "60")
        _save_kline_60_cache(sym, df)
        logging.info("kline_minute_sync: 已写 60m %s rows=%d", sym, len(df))
        return len(df)

    if period == "15":
        if src in ("a_share", "index"):
            sina_sym = _to_sina_symbol(sym, src, api_sym)
            raw = _fetch_15m_from_sina(sina_sym, start_ts, end_ts)
        elif src == "hk":
            try:
                raw = _fetch_hk_min_from_akshare(api_sym, start_ts, end_ts, period="15")
            except Exception:
                logging.exception("kline_minute_sync: 港股 15m AKShare 失败，回退 yfinance %s", api_sym)
                raw = _fetch_hk_min_from_yfinance(api_sym, start_ts, end_ts, interval="15m")
        else:
            raise ValueError(f"不支持标的: {sym}")
        df = _normalize_ohlcv_df(raw, "15")
        _save_kline_15_cache(sym, df)
        logging.info("kline_minute_sync: 已写 15m %s rows=%d", sym, len(df))
        return len(df)

    raise ValueError("period 仅支持 60 或 15")
