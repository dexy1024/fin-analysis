"""
60m / 15m K 线：仅从数据源拉取并写入 backend/data/kline_{60|15}_*.csv。

数据源优先级：
- A 股 / 多数 ETF / 指数：新浪 CN_MarketData.getKLineData
- 白名单 ETF（除权需缠论前复权，如 515050）：东财 fund_etf_hist_min_em qfq
- 港股：yfinance 优先，失败回退 AKShare（60m 可再回退 15m 聚合）

与 get_index_kline(period=60|15) 解耦——后者只读本地 CSV 并计算指标/缠论。
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import pandas as pd

from services.etf_em_qfq import etf_needs_em_qfq
from services.index_cache import CACHE_DIR
from services.indicators import (
    _kline_15_cache_path,
    _kline_60_cache_path,
    _aggregate_hk_15m_to_60m,
    _fetch_15m_from_sina,
    _fetch_60m_from_sina,
    _fetch_hk_60m_from_akshare,
    _fetch_hk_60m_from_yfinance,
    _fetch_hk_min_from_akshare,
    _fetch_hk_min_from_yfinance,
    _meihua2test_extend_end_ts_if_demo,
    _save_kline_15_cache,
    _save_kline_60_cache,
    _split_kline_symbol,
    _to_sina_symbol,
)
from services.kline_15_backfill_em import backfill_etf_min_em
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

PeriodMinute = Literal["60", "15"]

_HK_FETCH_GAP_SEC = 0.35


def _fetch_hk_minute_raw(
    api_sym: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    period: PeriodMinute,
) -> pd.DataFrame:
    """港股分钟线：yfinance 优先，AKShare 回退。"""
    if period == "60":
        try:
            return _fetch_hk_60m_from_yfinance(api_sym, start_ts, end_ts)
        except EXPECTED_BUSINESS_EXCEPTIONS:
            logging.warning("kline_minute_sync: 港股 60m yfinance 失败，回退 AKShare %s", api_sym, exc_info=True)
            time.sleep(_HK_FETCH_GAP_SEC)
            try:
                return _fetch_hk_60m_from_akshare(api_sym, start_ts, end_ts)
            except EXPECTED_BUSINESS_EXCEPTIONS:
                logging.warning("kline_minute_sync: 港股 60m AKShare 失败，尝试 15m 聚合 %s", api_sym, exc_info=True)
                time.sleep(_HK_FETCH_GAP_SEC)
                raw15 = _fetch_hk_min_from_akshare(api_sym, start_ts, end_ts, period="15")
                return _aggregate_hk_15m_to_60m(raw15)
            except Exception:
                logging.exception("kline_minute_sync: 港股 60m AKShare/聚合未预期异常 %s", api_sym)
                raise
        except Exception:
            logging.exception("kline_minute_sync: 港股 60m yfinance 未预期异常 %s", api_sym)
            raise
    try:
        return _fetch_hk_min_from_yfinance(api_sym, start_ts, end_ts, interval="15m")
    except EXPECTED_BUSINESS_EXCEPTIONS:
        logging.warning("kline_minute_sync: 港股 15m yfinance 失败，回退 AKShare %s", api_sym, exc_info=True)
        time.sleep(_HK_FETCH_GAP_SEC)
        return _fetch_hk_min_from_akshare(api_sym, start_ts, end_ts, period="15")
    except Exception:
        logging.exception("kline_minute_sync: 港股 15m yfinance 未预期异常 %s", api_sym)
        raise


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


def _ensure_etf_em_qfq_minute_cache(code: str, period: PeriodMinute) -> None:
    """白名单 ETF 首次改走东财 qfq 时，删除旧新浪分钟 CSV，避免复权混用。"""
    if not etf_needs_em_qfq(code):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker = CACHE_DIR / f".etf_em_qfq_{code.replace('/', '_')}_{period}"
    if marker.is_file():
        return
    path = _kline_60_cache_path(code) if period == "60" else _kline_15_cache_path(code)
    if path.is_file():
        path.unlink()
        logging.info("kline_minute_sync: 已删除旧分钟缓存 %s（改东财 qfq）", path.name)
    marker.touch()


def _fetch_etf_minute_em_qfq(
    api_sym: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    period: PeriodMinute,
) -> pd.DataFrame:
    end_s = end_ts.strftime("%Y-%m-%d %H:%M:%S")
    return backfill_etf_min_em(
        api_sym,
        start_ts.strftime("%Y-%m-%d"),
        end_s,
        period=period,
        chunk_calendar_days=14,
        sleep_sec=0.6,
    )


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

    if period == "60":
        if src == "a_share" and etf_needs_em_qfq(api_sym):
            _ensure_etf_em_qfq_minute_cache(api_sym, "60")
            raw = _fetch_etf_minute_em_qfq(api_sym, start_ts, end_ts, "60")
        elif src in ("a_share", "index"):
            sina_sym = _to_sina_symbol(sym, src, api_sym)
            raw = _fetch_60m_from_sina(sina_sym, start_ts, end_ts)
        elif src == "hk":
            raw = _fetch_hk_minute_raw(api_sym, start_ts, end_ts, "60")
            time.sleep(_HK_FETCH_GAP_SEC)
        else:
            raise ValueError(f"不支持标的: {sym}")
        df = _normalize_ohlcv_df(raw, "60")
        _save_kline_60_cache(sym, df)
        logging.info("kline_minute_sync: 已写 60m %s rows=%d", sym, len(df))
        return len(df)

    if period == "15":
        if src == "a_share" and etf_needs_em_qfq(api_sym):
            _ensure_etf_em_qfq_minute_cache(api_sym, "15")
            raw = _fetch_etf_minute_em_qfq(api_sym, start_ts, end_ts, "15")
        elif src in ("a_share", "index"):
            sina_sym = _to_sina_symbol(sym, src, api_sym)
            raw = _fetch_15m_from_sina(sina_sym, start_ts, end_ts)
        elif src == "hk":
            raw = _fetch_hk_minute_raw(api_sym, start_ts, end_ts, "15")
            time.sleep(_HK_FETCH_GAP_SEC)
        else:
            raise ValueError(f"不支持标的: {sym}")
        df = _normalize_ohlcv_df(raw, "15")
        _save_kline_15_cache(sym, df)
        logging.info("kline_minute_sync: 已写 15m %s rows=%d", sym, len(df))
        return len(df)

    raise ValueError("period 仅支持 60 或 15")
