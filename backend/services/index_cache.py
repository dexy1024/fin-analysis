"""
指数 / A 股 / ETF / 港股 日线本地缓存。
- A 股 / 多数 ETF / 指数：新浪 K 线（scale=240）
- 白名单 ETF（如 515050 除权）：东财 fund_etf_hist_em 前复权
- 港股：yfinance 日线优先，失败回退 AKShare
"""
from __future__ import annotations

import logging
from pathlib import Path
import time

import akshare as ak
import pandas as pd
import requests
import yfinance as yf

from services.etf_em_qfq import etf_needs_em_qfq
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

# backend/services -> backend/data
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"

INDEX_DAILY_ANCHOR = "2024-12-01"
SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)


def _a_share_daily_cache_path(code: str) -> Path:
    """股票 qfq 路径；一般 ETF 为 nq（新浪）；白名单 ETF 为东财 qfq。"""
    safe = code.replace("/", "_")
    if etf_needs_em_qfq(code):
        return CACHE_DIR / f"a_daily_qfq_{safe}.csv"
    if _is_likely_etf_code(code):
        return CACHE_DIR / f"a_daily_nq_{safe}.csv"
    return CACHE_DIR / f"a_daily_qfq_{safe}.csv"


def _is_likely_etf_code(code: str) -> bool:
    """场内 ETF：上海 51/56/58 开头，深圳 159 开头。"""
    if len(code) != 6 or not code.isdigit():
        return False
    if code.startswith(("51", "56", "58")):
        return True
    if code.startswith("159"):
        return True
    return False


def _with_retry(fetch_fn, *, retries: int = 3, sleep_sec: float = 0.7):
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            return fetch_fn()
        except EXPECTED_BUSINESS_EXCEPTIONS as exc:
            last_exc = exc
            if i < retries - 1:
                time.sleep(sleep_sec * (i + 1))
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


def _to_sina_symbol_for_a_share(code: str) -> str:
    return f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"


def _fetch_daily_from_sina_symbol(symbol: str) -> pd.DataFrame:
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR).normalize()
    resp = _with_retry(
        lambda: requests.get(
            SINA_KLINE_URL,
            params={"symbol": symbol.lower(), "scale": "240", "ma": "no", "datalen": "4096"},
            timeout=12,
        ),
    )
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"未获取到 {symbol} 的新浪日线数据")

    df = pd.DataFrame(rows)
    req = {"day", "open", "high", "low", "close", "volume"}
    if not req.issubset(df.columns):
        raise ValueError("新浪日线数据缺少必要字段")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["day"], errors="coerce").dt.normalize(),
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce"),
        },
    ).dropna(subset=["date", "open", "high", "low", "close", "volume"])
    out = out.sort_values("date").reset_index(drop=True)
    out = out[out["date"] >= anchor_ts].reset_index(drop=True)
    if out.empty:
        raise ValueError(f"{symbol} 新浪日线在指定区间无数据")
    return out


def _fetch_etf_daily_qfq(code: str) -> pd.DataFrame:
    """白名单 ETF 日线：东财前复权。"""
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR).normalize()
    end_s = pd.Timestamp.now().strftime("%Y%m%d")
    start_s = anchor_ts.strftime("%Y%m%d")

    def _call() -> pd.DataFrame:
        raw = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if raw is None or raw.empty:
            raise ValueError(f"东财未返回 ETF {code} 前复权日线")
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
        df = raw.rename(columns=rename)
        need = {"date", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            raise ValueError(f"ETF 日线缺少必要字段，实际列: {list(df.columns)}")
        out = df[list(need)].copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
        for col in ("open", "high", "low", "close", "volume"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
        out = out.sort_values("date").reset_index(drop=True)
        out = out[out["date"] >= anchor_ts].reset_index(drop=True)
        if out.empty:
            raise ValueError(f"ETF {code} 前复权日线在 {INDEX_DAILY_ANCHOR} 之后无数据")
        return out

    return _with_retry(_call)


def _fetch_a_share_daily(code: str) -> pd.DataFrame:
    """拉取 A 股/ETF 日线：白名单 ETF 东财 qfq，其余新浪。"""
    if etf_needs_em_qfq(code):
        return _fetch_etf_daily_qfq(code)
    return _fetch_daily_from_sina_symbol(_to_sina_symbol_for_a_share(code))


def load_a_share_daily_dataframe(code: str, *, force_refresh: bool = False) -> pd.DataFrame:
    """
    股票 / ETF 日线，自 INDEX_DAILY_ANCHOR 起缓存。
    统一使用新浪接口（scale=240）并落本地缓存。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _a_share_daily_cache_path(code)

    df_local: pd.DataFrame | None = None
    if path.exists() and not force_refresh:
        df_local = pd.read_csv(path, parse_dates=["date"])

    # 严格本地优先：仅在显式 force_refresh 或本地不存在时访问线上
    if force_refresh or df_local is None:
        raw = _fetch_a_share_daily(code)
        raw.to_csv(path, index=False)
        return raw

    assert df_local is not None
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR)
    out = df_local[pd.to_datetime(df_local["date"]) >= anchor_ts].reset_index(drop=True)
    return out


def sync_a_share_daily_cache_merged(code: str) -> pd.DataFrame:
    """
    拉取新浪日线并与本地 a_daily_*.csv 按 date 合并去重（keep last），再写回。
    用于增量更新：保留历史行，仅覆盖/追加与远端重叠及之后的日期。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _a_share_daily_cache_path(code)
    raw = _fetch_a_share_daily(code)
    raw["date"] = pd.to_datetime(raw["date"]).dt.normalize()
    if not path.exists():
        raw.to_csv(path, index=False)
        return raw
    try:
        local = pd.read_csv(path, parse_dates=["date"])
        local["date"] = pd.to_datetime(local["date"]).dt.normalize()
    except EXPECTED_BUSINESS_EXCEPTIONS:
        raw.to_csv(path, index=False)
        return raw
    except Exception:
        raise
    merged = pd.concat([local, raw], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR).normalize()
    merged = merged[merged["date"] >= anchor_ts].reset_index(drop=True)
    merged.to_csv(path, index=False)
    return merged


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_")
    return CACHE_DIR / f"index_daily_{safe}.csv"


def load_index_daily_dataframe(symbol: str, *, force_refresh: bool = False) -> pd.DataFrame:
    """
    返回从 INDEX_DAILY_ANCHOR 起的指数日线（新浪接口，scale=240）。
    严格本地优先：优先读本地 CSV；仅在 force_refresh 或本地不存在时拉取网络并写回。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)

    df_local: pd.DataFrame | None = None
    if path.exists() and not force_refresh:
        df_local = pd.read_csv(path, parse_dates=["date"])

    # 严格本地优先：仅在显式 force_refresh 或本地不存在时访问线上
    if force_refresh or df_local is None:
        raw = _fetch_daily_from_sina_symbol(symbol)
        raw.to_csv(path, index=False)
        return raw

    assert df_local is not None
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR)
    out = df_local[pd.to_datetime(df_local["date"]) >= anchor_ts].reset_index(drop=True)
    return out


def _hk_daily_cache_path(symbol: str) -> Path:
    """港股日线本地缓存路径，如 hk01810 -> hk_daily_hk01810.csv"""
    safe = symbol.replace("/", "_")
    return CACHE_DIR / f"hk_daily_{safe}.csv"


def _to_yfinance_hk_symbol(symbol: str) -> str:
    """5 位港股代码 -> yfinance 如 1810.HK。"""
    num = int(symbol)
    return f"{num:04d}.HK" if num < 1000 else f"{num}.HK"


def _fetch_hk_daily_from_yfinance_raw(symbol: str) -> pd.DataFrame:
    """港股日线：yfinance interval=1d。"""
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR).normalize()
    yf_sym = _to_yfinance_hk_symbol(symbol)
    logging.info("使用 yfinance 获取港股 %s 日线", symbol)

    def _call() -> pd.DataFrame:
        df = yf.Ticker(yf_sym).history(
            start=anchor_ts.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
        )
        if df is None or df.empty:
            raise ValueError(f"yfinance 未返回 {symbol} 港股日线")
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            },
        ).reset_index()
        time_col = next((c for c in ("Date", "Datetime") if c in df.columns), None)
        if time_col is None:
            raise ValueError(f"yfinance 港股日线缺少时间列: {list(df.columns)}")
        out = pd.DataFrame(
            {
                "date": pd.to_datetime(df[time_col], errors="coerce").dt.normalize(),
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "volume": pd.to_numeric(df["volume"], errors="coerce"),
            },
        )
        out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
        out = out.sort_values("date").reset_index(drop=True)
        out = out[out["date"] >= anchor_ts].reset_index(drop=True)
        if out.empty:
            raise ValueError(f"{symbol} yfinance 港股日线在 {INDEX_DAILY_ANCHOR} 之后无数据")
        return out

    return _with_retry(_call)


def _fetch_hk_daily_from_akshare_raw(symbol: str) -> pd.DataFrame:
    """
    从 AKShare 拉取港股完整日线数据。
    symbol: 5位数字代码，如 '01810'
    """
    df = ak.stock_hk_daily(symbol=symbol)
    if df is None or df.empty:
        raise ValueError(f"AKShare 未返回 {symbol} 的港股数据")
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _fetch_hk_daily_raw(api_sym: str) -> pd.DataFrame:
    """港股日线：yfinance 优先，AKShare 回退。"""
    try:
        return _fetch_hk_daily_from_yfinance_raw(api_sym)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("港股日线 yfinance 失败 %s，回退 AKShare: %s", api_sym, exc)
        return _fetch_hk_daily_from_akshare_raw(api_sym)
    except Exception:
        logging.exception("港股日线 yfinance 未预期异常 %s，回退 AKShare", api_sym)
        return _fetch_hk_daily_from_akshare_raw(api_sym)


def load_hk_daily_dataframe(symbol: str, *, force_refresh: bool = False) -> pd.DataFrame:
    """
    港股日线，自 INDEX_DAILY_ANCHOR 起缓存。
    严格本地优先；拉网时 yfinance 优先，失败回退 AKShare。
    symbol: 带 hk 前缀的代码，如 'hk01810'
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _hk_daily_cache_path(symbol)

    df_local: pd.DataFrame | None = None
    if path.exists() and not force_refresh:
        df_local = pd.read_csv(path, parse_dates=["date"])

    # 严格本地优先
    if force_refresh or df_local is None:
        api_sym = symbol[2:] if symbol.lower().startswith("hk") else symbol
        raw = _fetch_hk_daily_raw(api_sym)
        raw.to_csv(path, index=False)
        return raw

    assert df_local is not None
    anchor_ts = pd.to_datetime(INDEX_DAILY_ANCHOR)
    out = df_local[pd.to_datetime(df_local["date"]) >= anchor_ts].reset_index(drop=True)
    return out
