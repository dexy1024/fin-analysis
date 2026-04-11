from __future__ import annotations

import copy
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import akshare as ak
import pandas as pd
import requests

from services.index_cache import (
    _a_share_daily_cache_path,
    _cache_path,
    _is_likely_etf_code,
    load_a_share_daily_dataframe,
    load_index_daily_dataframe,
)

KLINE_60_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_KLINE_RESP_CACHE_TTL_SECONDS = 300


def _meihua2test_extend_end_ts_if_demo(
    symbol: str,
    period: str,
    default_end: pd.Timestamp,
) -> pd.Timestamp:
    """
    仅 symbol=889999（本项目内专用于梅花2test mock）：将 end_ts 扩展到本地 CSV 内最大时间，
    使「日历上晚于当前时刻」的 mock K 仍参与计算；不设环境变量也会生效。
    若需按线上口径截断 mock 文件，可设 MEIHUA2TEST_FUTURE_K=0|false|off。
    """
    if symbol.strip() != "889999":
        return default_end
    if os.environ.get("MEIHUA2TEST_FUTURE_K", "").strip().lower() in ("0", "false", "no", "off"):
        return default_end
    try:
        api_sym, src = _split_kline_symbol(symbol)
    except ValueError:
        return default_end

    mx: pd.Timestamp | None = None
    if period == "daily" and src == "a_share":
        path = _a_share_daily_cache_path(api_sym)
        if path.is_file():
            try:
                peek = pd.read_csv(path, parse_dates=["date"], usecols=["date"])
            except (ValueError, KeyError, pd.errors.EmptyDataError):
                peek = None
            if peek is not None and not peek.empty:
                mx = pd.to_datetime(peek["date"], errors="coerce").max()
    elif period == "60":
        path = _kline_60_cache_path(symbol)
        if path.is_file():
            try:
                peek = pd.read_csv(path, parse_dates=["date"], usecols=["date"])
            except (ValueError, KeyError, pd.errors.EmptyDataError):
                peek = None
            if peek is not None and not peek.empty:
                mx = pd.to_datetime(peek["date"], errors="coerce").max()

    if mx is None or pd.isna(mx):
        return default_end
    if getattr(mx, "tzinfo", None) is not None:
        mx = mx.tz_convert("Asia/Shanghai").tz_localize(None)
    if period == "daily":
        mx = mx.normalize()
    if mx > default_end:
        return mx
    return default_end
_KLINE_RESP_CACHE_MAX_ITEMS = 256
# (缓存写入时刻, 写入时本地 CSV 的 st_mtime, 响应体)；日线与 60m 分文件，任一侧更新只 purge 对应 period
_KLINE_RESP_CACHE: Dict[tuple[str, str, str, str], tuple[float, float | None, Dict[str, Any]]] = {}


def _kline_local_csv_mtime(symbol: str, period: str) -> float | None:
    """
    与 get_index_kline 数据源对应的本地文件 mtime；无本地文件（如港股日线直拉新浪）返回 None。
    """
    try:
        api_sym, src = _split_kline_symbol(symbol)
    except ValueError:
        return None
    pr = period.strip()
    if pr == "60":
        p = _kline_60_cache_path(symbol)
        return p.stat().st_mtime if p.is_file() else None
    if pr == "daily":
        if src == "index":
            p = _cache_path(api_sym)
        elif src == "a_share":
            p = _a_share_daily_cache_path(api_sym)
        else:
            return None
        return p.stat().st_mtime if p.is_file() else None
    return None


def _purge_stale_kline_cache_for_symbol_period(symbol: str, period: str) -> None:
    """本地 CSV 比缓存条目更新时，丢弃该 symbol+period 下全部响应缓存，触发重算分型/笔/中枢。"""
    cur = _kline_local_csv_mtime(symbol, period)
    if cur is None:
        return
    sym = symbol.strip()
    pr = period.strip()
    to_del: list[tuple[str, str, str, str]] = []
    for key in list(_KLINE_RESP_CACHE.keys()):
        k_sym, k_prd, _, _ = key
        if k_sym != sym or k_prd != pr:
            continue
        _ts, saved_src_mtime, _data = _KLINE_RESP_CACHE[key]
        if saved_src_mtime is None or cur > saved_src_mtime:
            to_del.append(key)
    for k in to_del:
        _KLINE_RESP_CACHE.pop(k, None)


def _kline_cache_delete_all_for_symbol_period(symbol: str, period: str) -> None:
    sym = symbol.strip()
    pr = period.strip()
    for key in list(_KLINE_RESP_CACHE.keys()):
        k_sym, k_prd, _, _ = key
        if k_sym == sym and k_prd == pr:
            _KLINE_RESP_CACHE.pop(key, None)


def _kline_cache_get(key: tuple[str, str, str, str]) -> Dict[str, Any] | None:
    item = _KLINE_RESP_CACHE.get(key)
    if item is None:
        return None
    ts, _src_mtime, data = item
    if time.time() - ts > _KLINE_RESP_CACHE_TTL_SECONDS:
        _KLINE_RESP_CACHE.pop(key, None)
        return None
    return copy.deepcopy(data)


def _kline_cache_set(
    key: tuple[str, str, str, str],
    data: Dict[str, Any],
    *,
    symbol: str,
    period: str,
) -> None:
    now = time.time()
    src_mt = _kline_local_csv_mtime(symbol, period)
    _KLINE_RESP_CACHE[key] = (now, src_mt, copy.deepcopy(data))
    # 轻量限长，避免进程常驻时缓存无限增长
    if len(_KLINE_RESP_CACHE) > _KLINE_RESP_CACHE_MAX_ITEMS:
        oldest_key = min(_KLINE_RESP_CACHE.items(), key=lambda x: x[1][0])[0]
        _KLINE_RESP_CACHE.pop(oldest_key, None)


def _refresh_daily_cache_for_kline_symbol(symbol: str) -> None:
    """
    60 分钟请求时顺带从网络刷新指数/A 股日线本地缓存，使日线与盘中行情同步，
    避免仅依赖「最后一根日期 < 今天」才更新导致的当日日线滞后。
    港股日线无此 CSV 缓存，跳过。
    """
    api_sym, src = _split_kline_symbol(symbol)
    if src == "index":
        load_index_daily_dataframe(api_sym, force_refresh=True)
    elif src == "a_share":
        load_a_share_daily_dataframe(api_sym, force_refresh=True)


def _kline_adjust_label(symbol: str) -> str:
    """API 返回的复权说明：指数/ETF 为 none；普通 A 股与港股为前复权 qfq。"""
    try:
        api_sym, src = _split_kline_symbol(symbol)
    except ValueError:
        return "none"
    if src == "index":
        return "none"
    if src == "hk":
        return "qfq"
    if src == "a_share":
        return "none" if _is_likely_etf_code(api_sym) else "qfq"
    return "none"


def _split_kline_symbol(symbol: str) -> tuple[str, str]:
    """
    返回 (api_symbol, source)。
    source 为 'index' 时使用 stock_zh_index_daily；'a_share' 为 A 股/ETF；
    'hk' 为港股 5 位代码（ak.stock_hk_hist / stock_hk_hist_min_em）。
    """
    s = symbol.strip()
    if not s:
        raise ValueError("symbol 不能为空")
    hm = re.fullmatch(r"(?i)hk(\d{5})$", s)
    if hm:
        return hm.group(1), "hk"
    if re.fullmatch(r"\d{5}", s):
        return s, "hk"
    if re.fullmatch(r"\d{6}", s):
        return s, "a_share"
    sl = s.lower()
    m = re.match(r"^(sh|sz)(\d{6})$", sl)
    if m:
        num = m.group(2)
        if m.group(1) == "sh" and num.startswith("000"):
            return sl, "index"
        if m.group(1) == "sz" and num.startswith("399"):
            return sl, "index"
        return num, "a_share"
    raise ValueError(
        f"不支持的 symbol: {symbol}（6 位 A 股/ETF、5 位港股如 01810、或 sh/sz+6 位指数）",
    )


def _with_retry(fetch_fn, *, retries: int = 3, sleep_sec: float = 0.8):
    """
    东财接口偶发 ProxyError/RemoteDisconnected，给 60m 拉取增加轻量重试，
    避免一次瞬时网络抖动直接返回 500。
    """
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            return fetch_fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < retries - 1:
                time.sleep(sleep_sec * (i + 1))
    assert last_exc is not None
    raise last_exc


def _kline_60_cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_")
    return KLINE_60_CACHE_DIR / f"kline_60_{safe}.csv"


def _save_kline_60_cache(symbol: str, df: pd.DataFrame) -> None:
    """
    缓存 60m 原始 OHLCV，供网络抖动时兜底。
    """
    if df.empty:
        return
    KLINE_60_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    keep = ["date", "open", "high", "low", "close", "volume"]
    if not set(keep).issubset(out.columns):
        return
    out = out[keep].copy()
    out.to_csv(_kline_60_cache_path(symbol), index=False)


def _load_kline_60_cache(symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame | None:
    path = _kline_60_cache_path(symbol)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None
    req = {"date", "open", "high", "low", "close", "volume"}
    if not req.issubset(df.columns):
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 防止误用历史“日线映射 60m”伪缓存（全是 15:00 的错误时间轴）
    hm = set(df["date"].dt.strftime("%H:%M"))
    if hm == {"15:00"}:
        return None
    df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)].reset_index(drop=True)
    return df if not df.empty else None


def _fetch_60m_from_sina(symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """
    新浪 60m 指数接口（更稳定）：
    https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
    """
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": symbol.lower(),  # sh000001 / sz000423 / sh510300 / hk01810
        "scale": "60",
        "ma": "no",
        "datalen": "2048",
    }

    r = _with_retry(lambda: requests.get(url, params=params, timeout=12), retries=3, sleep_sec=0.7)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"未获取到 {symbol} 的新浪60分钟数据")

    df = pd.DataFrame(rows)
    if not {"day", "open", "high", "low", "close", "volume"}.issubset(df.columns):
        raise ValueError("新浪60分钟数据缺少必要字段")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["day"]),
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce"),
        },
    )
    out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    out = out.sort_values("date").reset_index(drop=True)
    out = out[(out["date"] >= start_ts) & (out["date"] <= end_ts)].reset_index(drop=True)
    if out.empty:
        raise ValueError(f"{symbol} 新浪60分钟在指定区间无数据")
    return out


def _fetch_daily_from_sina(symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """
    新浪 K 线接口按 240 分钟获取日线口径数据（非聚合）。
    """
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": symbol.lower(),
        "scale": "240",
        "ma": "no",
        "datalen": "4096",
    }
    r = _with_retry(lambda: requests.get(url, params=params, timeout=12), retries=3, sleep_sec=0.7)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"未获取到 {symbol} 的新浪日线数据")

    df = pd.DataFrame(rows)
    if not {"day", "open", "high", "low", "close", "volume"}.issubset(df.columns):
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
    )
    out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    out = out.sort_values("date").reset_index(drop=True)
    out = out[(out["date"] >= start_ts.normalize()) & (out["date"] <= end_ts.normalize())].reset_index(drop=True)
    if out.empty:
        raise ValueError(f"{symbol} 新浪日线在指定区间无数据")
    return out


def _to_sina_symbol(symbol: str, src: str, api_sym: str) -> str:
    if src == "index":
        return symbol.lower()
    if src == "a_share":
        # A 股/ETF：5/6/9 开头通常上海，其余深圳
        return f"sh{api_sym}" if api_sym.startswith(("5", "6", "9")) else f"sz{api_sym}"
    if src == "hk":
        return f"hk{api_sym}"
    raise ValueError("不支持的 symbol")




def _normalize_symbol(code: str) -> str:
    """
    兼容用户只输入 6 位代码的情况，直接返回给 akshare 使用。
    ak.stock_zh_a_hist 当前支持直接传入 6 位代码，如 '600000' 或 '000001'。
    """
    code = code.strip()
    if not code:
        raise ValueError("股票代码不能为空")
    return code


def _calc_macd(close: pd.Series) -> pd.DataFrame:
    ema_short = close.ewm(span=12, adjust=False).mean()
    ema_long = close.ewm(span=26, adjust=False).mean()
    dif = ema_short - ema_long
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "macd": macd})


def _calc_boll(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    ma = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = ma + num_std * std
    lower = ma - num_std * std
    return pd.DataFrame({"upper": upper, "middle": ma, "lower": lower})


def _calc_kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 9,
) -> pd.DataFrame:
    low_min = low.rolling(window=period, min_periods=period).min()
    high_max = high.rolling(window=period, min_periods=period).max()
    rsv = (close - low_min) / (high_max - low_min) * 100

    # 使用 EWM 近似 KDJ 公式：K, D 为 RSV 的指数平滑
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"k": k, "d": d, "j": j})


def _has_inclusive_relation(prev_bar: Dict[str, Any], cur_bar: Dict[str, Any]) -> bool:
    prev_high, prev_low = float(prev_bar["high"]), float(prev_bar["low"])
    cur_high, cur_low = float(cur_bar["high"]), float(cur_bar["low"])
    return (cur_high <= prev_high and cur_low >= prev_low) or (cur_high >= prev_high and cur_low <= prev_low)


def _earlier_date(d1: Any, d2: Any) -> Any:
    """合并后极值相同时，取更早那根 K 线的日期（实盘习惯）。"""
    return d1 if pd.to_datetime(d1) <= pd.to_datetime(d2) else d2


def _to_yyyy_mm_dd(date_val: Any) -> str:
    """统一为 YYYY-MM-DD，供分型日期与行情 rows 对齐。"""
    if isinstance(date_val, pd.Timestamp):
        return date_val.strftime("%Y-%m-%d")
    s = str(date_val)
    if " " in s:
        return s.split(" ", 1)[0][:10]
    return s[:10] if len(s) >= 10 else s


def _axis_date_key(date_val: Any) -> str:
    """
    与 K 线 rows[].date 一致：纯日线为 YYYY-MM-DD，分钟线为 YYYY-MM-DD HH:MM。
    供分型、笔、中枢与原始 K 线下标映射使用（避免分钟线被截成同日而冲突）。
    """
    ts = pd.to_datetime(date_val)
    if (
        ts.hour == 0
        and ts.minute == 0
        and int(ts.second) == 0
        and getattr(ts, "microsecond", 0) == 0
    ):
        return ts.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%d %H:%M")


def _merge_contribute_extreme_dates(
    last: Dict[str, Any],
    bar: Dict[str, Any],
    trend: str,
) -> tuple[float, float, Any, Any]:
    """
    按包含合并规则得到新高/新低，并返回极值实际所在的原始 K 线日期（high_date / low_date）。
    与 _merge_inclusive_bars 内 up/down 分支的 merged_high、merged_low 计算一致。
    """
    lh, bh = float(last["high"]), float(bar["high"])
    ll, bl = float(last["low"]), float(bar["low"])
    ld_h = last.get("high_date", last["date"])
    bd_h = bar.get("high_date", bar["date"])
    ld_l = last.get("low_date", last["date"])
    bd_l = bar.get("low_date", bar["date"])

    if trend == "up":
        merged_high = max(lh, bh)
        merged_low = max(ll, bl)
        if lh > bh:
            high_date = ld_h
        elif bh > lh:
            high_date = bd_h
        else:
            high_date = _earlier_date(ld_h, bd_h)
        if ll > bl:
            low_date = ld_l
        elif bl > ll:
            low_date = bd_l
        else:
            low_date = _earlier_date(ld_l, bd_l)
    else:
        merged_high = min(lh, bh)
        merged_low = min(ll, bl)
        if lh < bh:
            high_date = ld_h
        elif bh < lh:
            high_date = bd_h
        else:
            high_date = _earlier_date(ld_h, bd_h)
        if ll < bl:
            low_date = ld_l
        elif bl < ll:
            low_date = bd_l
        else:
            low_date = _earlier_date(ld_l, bd_l)

    return merged_high, merged_low, high_date, low_date


def _merge_inclusive_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    处理K线包含关系，得到标准化K线序列。
    方向判定参考最近两根标准化K线；上升取高高低高，下降取高低低低。
    每根标准化 K 额外维护 high_date / low_date：当前 high、low 所对应的「实际极值」所在原始 K 线日期，
    供分型标注与实盘画线一致（极值看真创出高低的那根，而非合并后的末根日期）。
    """
    standardized: List[Dict[str, Any]] = []
    for bar in bars:
        if not standardized:
            b = bar.copy()
            d = b["date"]
            b["high_date"] = d
            b["low_date"] = d
            standardized.append(b)
            continue

        last = standardized[-1]
        if not _has_inclusive_relation(last, bar):
            b = bar.copy()
            d = b["date"]
            b["high_date"] = d
            b["low_date"] = d
            standardized.append(b)
            continue

        trend = "up"
        if len(standardized) >= 2:
            prev = standardized[-2]
            if float(last["high"]) > float(prev["high"]) and float(last["low"]) > float(prev["low"]):
                trend = "up"
            elif float(last["high"]) < float(prev["high"]) and float(last["low"]) < float(prev["low"]):
                trend = "down"
            else:
                trend = "up" if float(bar["close"]) >= float(last["close"]) else "down"
        else:
            trend = "up" if float(bar["close"]) >= float(last["close"]) else "down"

        merged_high, merged_low, high_date, low_date = _merge_contribute_extreme_dates(last, bar, trend)

        last.update(
            {
                "date": bar["date"],
                "high": merged_high,
                "low": merged_low,
                "close": bar["close"],
                "volume": float(last["volume"]) + float(bar["volume"]),
                "high_date": high_date,
                "low_date": low_date,
            },
        )

    return standardized


def _find_fractals_from_standardized(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    分型规则（核心3根，允许扩展到>=3根）:
    1) 先看核心三根（左-中-右）是否满足分型条件；
    2) 若满足，则向左右连续扩展，只要中间K线仍保持“极值点”约束就纳入同一有效区间；
       这对应“可以多于3根，但核心看中间K线”的判定思路。

    顶分型:
      - 中间最高点 > 左右任一根最高点
      - 中间最低点 >= 左右任一根最低点
    底分型:
      - 中间最低点 < 左右任一根最低点
      - 中间最高点 <= 左右任一根最高点
    """
    fractals: List[Dict[str, Any]] = []
    if len(bars) < 3:
        return fractals

    for i in range(1, len(bars) - 1):
        left = bars[i - 1]
        mid = bars[i]
        right = bars[i + 1]

        lh, ll = float(left["high"]), float(left["low"])
        mh, ml = float(mid["high"]), float(mid["low"])
        rh, rl = float(right["high"]), float(right["low"])

        # 核心三根判定（最小3根）
        core_top = mh > lh and mh > rh and ml >= ll and ml >= rl
        core_bottom = ml < ll and ml < rl and mh <= lh and mh <= rh

        # 允许扩展到多于3根：只要两侧连续K线仍满足“中间为极值”的约束即可
        # 顶分型扩展约束：other.high < mh 且 other.low <= ml
        # 底分型扩展约束：other.low > ml 且 other.high >= mh
        is_top = core_top
        is_bottom = core_bottom

        if core_top:
            left_ok = 1
            j = i - 2
            while j >= 0:
                hj, lj = float(bars[j]["high"]), float(bars[j]["low"])
                if hj < mh and lj <= ml:
                    left_ok += 1
                    j -= 1
                else:
                    break

            right_ok = 1
            j = i + 2
            while j < len(bars):
                hj, lj = float(bars[j]["high"]), float(bars[j]["low"])
                if hj < mh and lj <= ml:
                    right_ok += 1
                    j += 1
                else:
                    break

            is_top = left_ok >= 1 and right_ok >= 1

        if core_bottom:
            left_ok = 1
            j = i - 2
            while j >= 0:
                hj, lj = float(bars[j]["high"]), float(bars[j]["low"])
                if lj > ml and hj >= mh:
                    left_ok += 1
                    j -= 1
                else:
                    break

            right_ok = 1
            j = i + 2
            while j < len(bars):
                hj, lj = float(bars[j]["high"]), float(bars[j]["low"])
                if lj > ml and hj >= mh:
                    right_ok += 1
                    j += 1
                else:
                    break

            is_bottom = left_ok >= 1 and right_ok >= 1

        # 分型日期：顶看实际创高 K（high_date），底看实际创低 K（low_date），与缠论实盘画线一致
        if is_top:
            ext_date = mid.get("high_date", mid["date"])
            fractals.append(
                {"type": "top", "date": _axis_date_key(ext_date), "price": mh, "bar_index": i},
            )
        elif is_bottom:
            ext_date = mid.get("low_date", mid["date"])
            fractals.append(
                {"type": "bottom", "date": _axis_date_key(ext_date), "price": ml, "bar_index": i},
            )

    return fractals


def _normalize_fractals_for_bi(fractals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    先把有效分型按顺序做“同类合并”，保证后续按交替类型配对生成笔。
    - 连续顶分型保留更高的顶；
    - 连续底分型保留更低的底。
    """
    if not fractals:
        return []

    normalized: List[Dict[str, Any]] = [fractals[0].copy()]
    for cur in fractals[1:]:
        last = normalized[-1]
        if cur["type"] != last["type"]:
            normalized.append(cur.copy())
            continue

        if cur["type"] == "top":
            if float(cur["price"]) >= float(last["price"]):
                normalized[-1] = cur.copy()
        else:  # bottom
            if float(cur["price"]) <= float(last["price"]):
                normalized[-1] = cur.copy()

    return normalized


def _fractal_date_key(date_val: Any) -> str:
    """分型日期与行情 rows 的 date 对齐，便于在原始 K 线上查下标。"""
    return _axis_date_key(date_val)


def _raw_date_index_map(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """原始（未合并）K 线顺序下，每个交易日 -> 下标。"""
    m: Dict[str, int] = {}
    for i, row in enumerate(rows):
        m[_fractal_date_key(row["date"])] = i
    return m


def _fractal_pair_gap_ok(
    left: Dict[str, Any],
    right: Dict[str, Any],
    raw_date_index: Dict[str, int] | None,
) -> bool:
    """
    两分型之间至少隔 1 根独立 K 线：下标差 >= 2。
    若提供 raw_date_index，则按「图表上的原始日线」下标判断，避免合并包含关系后
    标准化序列里相邻、但原始走势中间仍有独立 K 的情况漏笔。
    """
    if raw_date_index is not None:
        lk = _fractal_date_key(left["date"])
        rk = _fractal_date_key(right["date"])
        if lk in raw_date_index and rk in raw_date_index:
            return raw_date_index[rk] - raw_date_index[lk] >= 2
    return int(right["bar_index"]) - int(left["bar_index"]) >= 2


def _build_bi_from_fractals(
    fractals: List[Dict[str, Any]],
    raw_date_index: Dict[str, int] | None = None,
) -> List[Dict[str, Any]]:
    """
    按缠论“笔”的基础规则由分型生成笔:
    1) 仅允许相邻分型配对，且类型必须交替（底->顶 或 顶->底）；
    2) 两个分型中心K线之间至少间隔1根独立K线（下标差 >= 2）；
       若传入 raw_date_index，则按原始日线序列下标判断，与前端 K 线图一致；
    3) 向上笔: 起点=底分型最低点, 终点=顶分型最高点；
       向下笔: 起点=顶分型最高点, 终点=底分型最低点。
    """
    bi_list: List[Dict[str, Any]] = []
    if len(fractals) < 2:
        return bi_list

    normalized = _normalize_fractals_for_bi(fractals)
    if len(normalized) < 2:
        return bi_list

    for i in range(len(normalized) - 1):
        left = normalized[i]
        right = normalized[i + 1]

        if left["type"] == right["type"]:
            continue
        if not _fractal_pair_gap_ok(left, right, raw_date_index):
            continue

        if left["type"] == "bottom" and right["type"] == "top":
            bi_list.append(
                {
                    "direction": "up",
                    "start_date": left["date"],
                    "start_price": float(left["price"]),
                    "end_date": right["date"],
                    "end_price": float(right["price"]),
                },
            )
        elif left["type"] == "top" and right["type"] == "bottom":
            bi_list.append(
                {
                    "direction": "down",
                    "start_date": left["date"],
                    "start_price": float(left["price"]),
                    "end_date": right["date"],
                    "end_price": float(right["price"]),
                },
            )

    return bi_list


def _pen_price_range(pen: Dict[str, Any]) -> tuple[float, float]:
    low = min(float(pen["start_price"]), float(pen["end_price"]))
    high = max(float(pen["start_price"]), float(pen["end_price"]))
    return low, high


def _three_pens_overlap(p1: Dict[str, Any], p2: Dict[str, Any], p3: Dict[str, Any]) -> bool:
    l1, h1 = _pen_price_range(p1)
    l2, h2 = _pen_price_range(p2)
    l3, h3 = _pen_price_range(p3)
    overlap_low = max(l1, l2, l3)
    overlap_high = min(h1, h2, h3)
    return overlap_low <= overlap_high


def _pen_to_bar_dict(pen: Dict[str, Any]) -> Dict[str, Any]:
    """将单笔视为一根 K（高低为笔的价域），供包含关系合并。"""
    s = float(pen["start_price"])
    e = float(pen["end_price"])
    return {
        "date": pd.to_datetime(pen["end_date"]),
        "open": s,
        "high": max(s, e),
        "low": min(s, e),
        "close": e,
        "volume": 0.0,
    }


def _triple_overlap_after_inclusive(p1: Dict[str, Any], p2: Dict[str, Any], p3: Dict[str, Any]) -> bool:
    """
    三笔是否满足「有重叠区」：先将三笔各视作一根 K，做与 K 线相同的包含合并，
    再判断合并后是否仍存在公共价域重叠（合并为 1 根则视为强包含，满足重叠）。
    """
    merged = _merge_inclusive_bars([_pen_to_bar_dict(p1), _pen_to_bar_dict(p2), _pen_to_bar_dict(p3)])
    if len(merged) == 1:
        return True
    if len(merged) == 2:
        l1, h1 = float(merged[0]["low"]), float(merged[0]["high"])
        l2, h2 = float(merged[1]["low"]), float(merged[1]["high"])
        return max(l1, l2) <= min(h1, h2)
    return _three_pens_overlap(p1, p2, p3)


def _normalize_effective_pens(src: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    有效笔：方向交替；连续同向笔合并为更极端的一根（与笔生成一致）。
    """
    if not src:
        return []
    out: List[Dict[str, Any]] = [src[0].copy()]
    for cur in src[1:]:
        last = out[-1]
        if cur["direction"] != last["direction"]:
            out.append(cur.copy())
            continue
        if cur["direction"] == "up":
            if float(cur["end_price"]) >= float(last["end_price"]):
                out[-1] = cur.copy()
        else:
            if float(cur["end_price"]) <= float(last["end_price"]):
                out[-1] = cur.copy()
    return out


def _triple_valid_for_segment(p1: Dict[str, Any], p2: Dict[str, Any], p3: Dict[str, Any]) -> bool:
    alt = p1["direction"] != p2["direction"] and p2["direction"] != p3["direction"]
    return bool(alt and _triple_overlap_after_inclusive(p1, p2, p3))


def _segment_polyline_points(
    pens: List[Dict[str, Any]],
    start_i: int,
    end_i: int,
) -> List[List[Any]]:
    """沿线段内笔的顶底端点依次转折，避免一根长弦直连首尾。"""
    pts: List[List[Any]] = []
    first = pens[start_i]
    pts.append([str(first["start_date"]), float(first["start_price"])])
    for k in range(start_i, end_i + 1):
        p = pens[k]
        last_pt = pts[-1]
        end_d = str(p["end_date"])
        end_v = float(p["end_price"])
        if last_pt[0] == end_d and abs(float(last_pt[1]) - end_v) < 1e-9:
            continue
        pts.append([end_d, end_v])
    return pts


def _support_from_feature_sequence_down_pens(fs_bars: List[Dict[str, Any]]) -> float | None:
    """
    向上线段对应的特征序列：线段内各向下笔视为 K，先包含合并，再取底分型或最低价为支撑。
    """
    if not fs_bars:
        return None
    merged = _merge_inclusive_bars(fs_bars)
    if not merged:
        return None
    if len(merged) >= 3:
        frs = _find_fractals_from_standardized(merged)
        bottoms = [float(f["price"]) for f in frs if f["type"] == "bottom"]
        if bottoms:
            return bottoms[-1]
    return min(float(b["low"]) for b in merged)


def _resistance_from_feature_sequence_up_pens(fs_bars: List[Dict[str, Any]]) -> float | None:
    """
    向下线段对应的特征序列：线段内各向上笔视为 K，先包含合并，再取顶分型或最高价为阻力。
    """
    if not fs_bars:
        return None
    merged = _merge_inclusive_bars(fs_bars)
    if not merged:
        return None
    if len(merged) >= 3:
        frs = _find_fractals_from_standardized(merged)
        tops = [float(f["price"]) for f in frs if f["type"] == "top"]
        if tops:
            return tops[-1]
    return max(float(b["high"]) for b in merged)


def _up_segment_feature_sequence_break(ep: List[Dict[str, Any]], seg_start_i: int, j: int) -> bool:
    """
    向上线段是否在 j 处被终结：除反向三笔重叠外，候选向下三笔的最低价须跌破
    特征序列（向下笔经包含合并后）上最后一个底分型/支撑，才算破坏。
    """
    fs_bars: List[Dict[str, Any]] = []
    for k in range(seg_start_i, j):
        if ep[k]["direction"] == "down":
            fs_bars.append(_pen_to_bar_dict(ep[k]))
    if not fs_bars:
        return False
    support = _support_from_feature_sequence_down_pens(fs_bars)
    if support is None:
        return False
    low_break = min(
        float(ep[j]["end_price"]),
        float(ep[j + 1]["end_price"]),
        float(ep[j + 2]["end_price"]),
    )
    return low_break < support - 1e-9


def _down_segment_feature_sequence_break(ep: List[Dict[str, Any]], seg_start_i: int, j: int) -> bool:
    """向下线段：特征序列为向上笔；反向三笔最高价须突破合并后最后顶分型/阻力。"""
    fs_bars: List[Dict[str, Any]] = []
    for k in range(seg_start_i, j):
        if ep[k]["direction"] == "up":
            fs_bars.append(_pen_to_bar_dict(ep[k]))
    if not fs_bars:
        return False
    resistance = _resistance_from_feature_sequence_up_pens(fs_bars)
    if resistance is None:
        return False
    high_break = max(
        float(ep[j]["end_price"]),
        float(ep[j + 1]["end_price"]),
        float(ep[j + 2]["end_price"]),
    )
    return high_break > resistance + 1e-9


def _build_segments_from_pens(pens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    线段（输入为已生成的笔）:
    1) 至少 3 根连续交替笔，且三笔经包含处理后仍有价域重叠；
    2) 向上线段：由向上笔起笔；延伸直至出现「反向向下线段」起点 j，且该三笔对前段特征序列（向下笔合并后分型）构成破坏；
    3) 向下线段：对称（特征序列为向上笔，破坏为向上突破阻力）；
    4) 折线点 points 沿笔端点逐笔转折；
    5) 仅三笔重叠但不足以破坏特征序列支撑/阻力时，不结束原线段（过滤噪音、减少无谓转折）。
    """
    segments: List[Dict[str, Any]] = []
    ep = _normalize_effective_pens(pens)
    n = len(ep)
    if n < 3:
        return segments

    def up_segment_start(i: int) -> bool:
        if i + 2 >= n:
            return False
        return ep[i]["direction"] == "up" and _triple_valid_for_segment(ep[i], ep[i + 1], ep[i + 2])

    def down_segment_start(i: int) -> bool:
        if i + 2 >= n:
            return False
        return ep[i]["direction"] == "down" and _triple_valid_for_segment(ep[i], ep[i + 1], ep[i + 2])

    def find_up_segment_break(i: int) -> int:
        """第一个同时满足：向下三笔重叠段起点 + 对向上线段特征序列分型破坏 的 j。"""
        for j in range(i + 3, n - 2):
            if down_segment_start(j) and _up_segment_feature_sequence_break(ep, i, j):
                return j
        return n

    def find_down_segment_break(i: int) -> int:
        """第一个同时满足：向上三笔重叠段起点 + 对向下线段特征序列破坏 的 j。"""
        for j in range(i + 3, n - 2):
            if up_segment_start(j) and _down_segment_feature_sequence_break(ep, i, j):
                return j
        return n

    pos = 0
    while pos < n:
        if pos + 2 >= n:
            break
        if up_segment_start(pos):
            brk = find_up_segment_break(pos)
            end_i = (brk - 1) if brk < n else (n - 1)
            if end_i < pos + 2:
                pos += 1
                continue
            peak: tuple[float, str] | None = None
            for k in range(pos, end_i + 1):
                if ep[k]["direction"] == "up":
                    pr = float(ep[k]["end_price"])
                    ds = str(ep[k]["end_date"])
                    if peak is None or pr > peak[0] or (abs(pr - peak[0]) < 1e-9 and ds > peak[1]):
                        peak = (pr, ds)
            if peak is None:
                pos += 1
                continue
            peak_price, peak_date = peak
            poly = _segment_polyline_points(ep, pos, end_i)
            segments.append(
                {
                    "direction": "up",
                    "points": poly,
                    "effective_pen_start_idx": pos,
                    "effective_pen_end_idx": end_i,
                    "start_date": str(ep[pos]["start_date"]),
                    "start_price": float(ep[pos]["start_price"]),
                    "end_date": peak_date,
                    "end_price": peak_price,
                    "pen_count": end_i - pos + 1,
                },
            )
            pos = brk
        elif down_segment_start(pos):
            brk = find_down_segment_break(pos)
            end_i = (brk - 1) if brk < n else (n - 1)
            if end_i < pos + 2:
                pos += 1
                continue
            trough: tuple[float, str] | None = None
            for k in range(pos, end_i + 1):
                if ep[k]["direction"] == "down":
                    pr = float(ep[k]["end_price"])
                    ds = str(ep[k]["end_date"])
                    if trough is None or pr < trough[0] or (abs(pr - trough[0]) < 1e-9 and ds > trough[1]):
                        trough = (pr, ds)
            if trough is None:
                pos += 1
                continue
            trough_price, trough_date = trough
            poly = _segment_polyline_points(ep, pos, end_i)
            segments.append(
                {
                    "direction": "down",
                    "points": poly,
                    "effective_pen_start_idx": pos,
                    "effective_pen_end_idx": end_i,
                    "start_date": str(ep[pos]["start_date"]),
                    "start_price": float(ep[pos]["start_price"]),
                    "end_date": trough_date,
                    "end_price": trough_price,
                    "pen_count": end_i - pos + 1,
                },
            )
            pos = brk
        else:
            pos += 1

    return segments


def _segment_dg(seg: Dict[str, Any]) -> tuple[float, float]:
    """线段价域 [d,g]：低、高。优先用折线 points。"""
    pts = seg.get("points") or []
    if len(pts) >= 2:
        prices = [float(p[1]) for p in pts]
        return min(prices), max(prices)
    s = float(seg["start_price"])
    e = float(seg["end_price"])
    return min(s, e), max(s, e)


def _date_min3(a: str, b: str, c: str) -> str:
    return min(a, b, c)


def _date_max3(a: str, b: str, c: str) -> str:
    return max(a, b, c)


def _pen_price_range(pen: Dict[str, Any]) -> tuple[float, float]:
    """单笔价域 [低, 高]（端点 min/max）。"""
    s = float(pen["start_price"])
    e = float(pen["end_price"])
    return min(s, e), max(s, e)


def _central_visual_end_date(
    bars: List[Dict[str, Any]],
    start_date: str,
    candidate_end: str,
    zd: float,
    zg: float,
) -> tuple[str, bool]:
    """
    中枢在图上的结束日：第一次出现收盘价离开 [zd,zg] 时，结束于此前最后一个仍在区间内的交易日。
    若区间内从未有收盘价落入 [zd,zg]，返回 (candidate_end, False)。
    若至 candidate_end 收盘价仍在区间内，返回 (candidate_end, True)。
    """
    start_k = _axis_date_key(start_date)
    end_k = _axis_date_key(candidate_end)
    last_inside: str | None = None
    for b in bars:
        ds = _axis_date_key(b["date"])
        if ds < start_k:
            continue
        if ds > end_k:
            break
        c = float(b["close"])
        inside = zd - 1e-9 <= c <= zg + 1e-9
        if inside:
            last_inside = ds
        elif last_inside is not None:
            return last_inside, True
    if last_inside is None:
        return end_k, False
    return end_k, True


def _macd_abs_area_between(bars: List[Dict[str, Any]], d_start: str, d_end: str) -> float:
    """笔区间 [d_start, d_end] 内 MACD 柱状图绝对值之和（力度近似）。"""
    k0 = _axis_date_key(d_start)
    k1 = _axis_date_key(d_end)
    s = 0.0
    for b in bars:
        ds = _axis_date_key(b["date"])
        if ds < k0:
            continue
        if ds > k1:
            break
        macd_obj = b.get("macd")
        if not isinstance(macd_obj, dict):
            continue
        v = macd_obj.get("macd")
        if v is None:
            continue
        try:
            s += abs(float(v))
        except (TypeError, ValueError):
            continue
    return s


# 去重键精度：2 位时低价 ETF（如 4 元档）上 0.01≈0.25%，易把相近中枢合并；3 位更细且指数价仍够用。
CENTRAL_DEDUPE_DECIMALS = 3


def _dedupe_centrals_by_zd_zg(centrals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """相同 [ZD,ZG]（按 CENTRAL_DEDUPE_DECIMALS 四舍五入）只保留 form_end_date 最晚的一条，避免重复画同一框。"""
    dec = CENTRAL_DEDUPE_DECIMALS
    best: Dict[tuple[float, float], Dict[str, Any]] = {}
    for c in centrals:
        key = (round(float(c["zd"]), dec), round(float(c["zg"]), dec))
        prev = best.get(key)
        if prev is None or str(c["form_end_date"]) > str(prev["form_end_date"]):
            best[key] = c
    return list(best.values())


def _central_distance_to_price(zd: float, zg: float, price: float) -> float:
    """当前价到中枢区间 [zd,zg] 的距离（在区间内为 0）。"""
    if zd - 1e-9 <= price <= zg + 1e-9:
        return 0.0
    if price < zd:
        return zd - price
    return price - zg


def _build_centrals(
    pens: List[Dict[str, Any]],
    last_close: float,
    bars: List[Dict[str, Any]],
    max_visible: int = 3,
) -> List[Dict[str, Any]]:
    """
    中枢：连续三笔（有效笔）端点价域满足 ZG=min(g)>ZD=max(d) 时生成。

    线段条数很少时，用整条折线 min/max 会导致 [ZD,ZG] 失真且时间被长线段拉满；
    故改用「笔」滑动窗口，并在日线上用收盘价首次有效离开区间裁剪可视结束日。
    去重见 CENTRAL_DEDUPE_DECIMALS（默认可区分低价 ETF 上更近的价位带）。
    """
    centrals: List[Dict[str, Any]] = []
    m = len(pens)
    if m < 3:
        return centrals

    for i in range(m - 2):
        p1, p2, p3 = pens[i], pens[i + 1], pens[i + 2]
        d1, g1 = _pen_price_range(p1)
        d2, g2 = _pen_price_range(p2)
        d3, g3 = _pen_price_range(p3)
        zg = min(g1, g2, g3)
        zd = max(d1, d2, d3)
        if zg <= zd + 1e-9:
            continue
        left_date = _date_min3(str(p1["start_date"]), str(p2["start_date"]), str(p3["start_date"]))
        candidate_end = _date_max3(str(p1["end_date"]), str(p2["end_date"]), str(p3["end_date"]))
        end_date, has_close_in_zone = _central_visual_end_date(bars, left_date, candidate_end, zd, zg)
        if not has_close_in_zone:
            continue
        # 进入中枢：构成中枢的第一笔；离开中枢：紧随三笔之后的下一笔（若存在）
        enter_pen = p1
        leave_pen = pens[i + 3] if i + 3 < m else None
        area_enter = _macd_abs_area_between(bars, str(enter_pen["start_date"]), str(enter_pen["end_date"]))
        if leave_pen is not None:
            area_leave = _macd_abs_area_between(bars, str(leave_pen["start_date"]), str(leave_pen["end_date"]))
            potential_divergence = bool(area_enter > 1e-9 and area_leave < area_enter)
        else:
            area_leave = 0.0
            potential_divergence = False
        centrals.append(
            {
                "zd": zd,
                "zg": zg,
                "start_date": left_date,
                "end_date": end_date,
                "form_end_date": str(p3["end_date"]),
                "segment_indices": [i, i + 1, i + 2],
                "extend_reason": "three_pens_close_clipped",
                "potential_divergence": potential_divergence,
                "macd_area_enter": round(area_enter, 6),
                "macd_area_leave": round(area_leave, 6) if leave_pen is not None else None,
            },
        )

    if not centrals:
        return []

    centrals = _dedupe_centrals_by_zd_zg(centrals)
    centrals.sort(key=lambda c: str(c["end_date"]), reverse=True)
    centrals.sort(key=lambda c: _central_distance_to_price(float(c["zd"]), float(c["zg"]), last_close))
    return centrals[:max_visible]


def get_latest_indicators(code: str) -> Dict[str, Any]:
    symbol = _normalize_symbol(code)

    # 获取较长一段时间的历史数据，保证技术指标有足够样本
    # 这里简单取从 2015-01-01 到今天
    today_str = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        start_date="20150101",
        end_date=today_str,
        adjust="qfq",
    )
    if df is None or df.empty:
        raise ValueError(f"未获取到股票 {symbol} 的历史数据")

    # 统一英文列名，便于后续处理
    # 常见列名：日期, 股票代码, 名称, 开盘, 收盘, 最高, 最低, 成交量, 成交额, ...
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    df = df.rename(columns=rename_map)

    required_cols = {"date", "open", "high", "low", "close", "volume"}
    if not required_cols.issubset(df.columns):
        raise ValueError("行情数据缺少必要字段，请检查 akshare 返回结果")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 60:
        raise ValueError("历史数据不足以计算技术指标，请选择有更长历史的股票")

    macd_df = _calc_macd(df["close"])
    boll_df = _calc_boll(df["close"])
    kdj_df = _calc_kdj(df["high"], df["low"], df["close"])

    indicators = pd.concat([df[["date", "close", "volume"]], macd_df, boll_df, kdj_df], axis=1)
    latest = indicators.iloc[-1]

    latest_date = latest["date"]
    if isinstance(latest_date, pd.Timestamp):
        latest_date_str = latest_date.strftime("%Y-%m-%d")
    else:
        latest_date_str = str(latest_date)

    return {
        "code": symbol,
        "date": latest_date_str,
        "close": float(latest["close"]),
        "volume": float(latest["volume"]),
        "macd": {
            "dif": float(latest["dif"]),
            "dea": float(latest["dea"]),
            "macd": float(latest["macd"]),
        },
        "boll": {
            "upper": float(latest["upper"]),
            "middle": float(latest["middle"]),
            "lower": float(latest["lower"]),
        },
        "kdj": {
            "k": float(latest["k"]),
            "d": float(latest["d"]),
            "j": float(latest["j"]),
        },
    }


def get_history_indicators(code: str, start_date: str = "2026-01-01") -> Dict[str, Any]:
    symbol = _normalize_symbol(code)

    today_str = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        start_date="20150101",
        end_date=today_str,
        adjust="qfq",
    )
    if df is None or df.empty:
        raise ValueError(f"未获取到股票 {symbol} 的历史数据")

    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    df = df.rename(columns=rename_map)

    required_cols = {"date", "open", "high", "low", "close", "volume"}
    if not required_cols.issubset(df.columns):
        raise ValueError("行情数据缺少必要字段，请检查 akshare 返回结果")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 60:
        raise ValueError("历史数据不足以计算技术指标，请选择有更长历史的股票")

    macd_df = _calc_macd(df["close"])
    boll_df = _calc_boll(df["close"])
    kdj_df = _calc_kdj(df["high"], df["low"], df["close"])

    indicators = pd.concat([df[["date", "close", "volume"]], macd_df, boll_df, kdj_df], axis=1)

    start_ts = pd.to_datetime(start_date)
    indicators = indicators[indicators["date"] >= start_ts].reset_index(drop=True)
    if indicators.empty:
        raise ValueError("指定起始日期之后没有可用数据")

    rows: List[Dict[str, Any]] = []
    for _, row in indicators.iterrows():
        ts = row["date"]
        date_str = ts.strftime("%Y-%m-%d") if isinstance(ts, pd.Timestamp) else str(ts)
        rows.append(
            {
                "date": date_str,
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "macd": {
                    "dif": float(row["dif"]),
                    "dea": float(row["dea"]),
                    "macd": float(row["macd"]),
                },
                "boll": {
                    "upper": float(row["upper"]),
                    "middle": float(row["middle"]),
                    "lower": float(row["lower"]),
                },
                "kdj": {
                    "k": float(row["k"]),
                    "d": float(row["d"]),
                    "j": float(row["j"]),
                },
            },
        )

    return {"code": symbol, "data": rows}


def get_index_kline(
    symbol: str = "sh000001",
    start_date: str = "2024-12-01",
    end_date: str | None = None,
    period: str = "daily",
    refresh: bool = False,
) -> Dict[str, Any]:
    """
    返回 K 线及缠论衍生字段（分型/笔/线段/有效笔/至多 3 段中枢）。

    缓存与本地文件（日线与 60m 分开判断）：
    - refresh=False 时，若本周期对应本地 CSV 的修改时间晚于该 symbol+period 下已缓存响应所记录的时间，
      会先丢弃该标的该周期全部内存缓存再重算；否则在 TTL（默认 300s）内可命中缓存。
    - 日线 CSV：指数 index_daily_*.csv，A 股/ETF 为 a_daily_qfq_*.csv / a_daily_nq_*.csv。
    - 60m CSV：data/kline_60_{symbol}.csv。
    - 港股日线无本地 CSV，不参与 mtime 比对，仅依赖 TTL。
    - refresh=True 时清空该标的该周期全部缓存并强制走拉数/读盘后的完整计算。
    """
    cache_key: tuple[str, str, str, str] = (
        symbol.strip(),
        period.strip(),
        start_date.strip(),
        (end_date or "").strip(),
    )
    if not refresh:
        _purge_stale_kline_cache_for_symbol_period(symbol, period)
        cached = _kline_cache_get(cache_key)
        if cached is not None:
            return cached
    else:
        # refresh=true 时清掉该标的该周期下全部响应缓存，避免不同 start_date 键残留旧中枢
        _kline_cache_delete_all_for_symbol_period(symbol, period)

    start_ts = pd.to_datetime(start_date)
    # 日线：起止一律按「日历日 0 点」比较；60 分钟保持原有时分秒逻辑
    if period == "daily":
        start_ts = start_ts.normalize()
        end_ts = pd.to_datetime(end_date).normalize() if end_date else pd.Timestamp.today().normalize()
    elif end_date:
        end_ts = pd.to_datetime(end_date)
        # 60 分钟：若只给到自然日 0 点，数据源会截断当日盘中 K，改为该日收盘前
        if period == "60" and end_ts.normalize() == end_ts:
            end_ts = end_ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    elif period == "60":
        # 未指定 end 时须用「当前时刻」，勿用 normalize()（否则 end 落在当日 0 点，盘中已走出的 60m 根会被排除）
        end_ts = pd.Timestamp.now()
    else:
        end_ts = pd.Timestamp.today().normalize()

    end_ts = _meihua2test_extend_end_ts_if_demo(symbol, period, end_ts)

    if end_ts < start_ts:
        raise ValueError("end_date 不能早于 start_date")

    if period == "daily":
        api_sym, src = _split_kline_symbol(symbol)
        if src == "index":
            df = load_index_daily_dataframe(api_sym, force_refresh=refresh)
        elif src == "hk":
            sina_symbol = _to_sina_symbol(symbol, src, api_sym)
            df = _fetch_daily_from_sina(sina_symbol, start_ts, end_ts)
        else:
            df = load_a_share_daily_dataframe(api_sym, force_refresh=refresh)
        df["date"] = pd.to_datetime(df["date"])
        if getattr(df["date"].dtype, "tz", None) is not None:
            df["date"] = df["date"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        df["date"] = df["date"].dt.normalize()
        df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)].reset_index(drop=True)
        macd_daily = _calc_macd(df["close"])
        df = pd.concat([df, macd_daily], axis=1)
        boll_daily = _calc_boll(df["close"], period=20, num_std=2.0)
        df = pd.concat([df, boll_daily], axis=1)
    elif period == "60":
        api_sym, src = _split_kline_symbol(symbol)
        cached = _load_kline_60_cache(symbol, start_ts, end_ts)

        def fetch_remote_60m() -> pd.DataFrame:
            if src == "a_share":
                sina_symbol = _to_sina_symbol(symbol, src, api_sym)
                return _fetch_60m_from_sina(sina_symbol, start_ts, end_ts)
            if src == "index":
                sina_symbol = _to_sina_symbol(symbol, src, api_sym)
                return _fetch_60m_from_sina(sina_symbol, start_ts, end_ts)
            if src == "hk":
                sina_symbol = _to_sina_symbol(symbol, src, api_sym)
                return _fetch_60m_from_sina(sina_symbol, start_ts, end_ts)
            raise ValueError("不支持的 symbol")

        # 默认严格只读本地；仅 refresh=true 时访问线上
        if not refresh:
            if cached is None:
                raise ValueError(f"{symbol} 本地60分钟缓存不存在，请先用 refresh=true 预拉数据")
            df = cached
            logging.info("60m 命中本地缓存: %s (rows=%s)", symbol, len(df))
        else:
            try:
                # 仅显式 refresh 时顺带刷新日线缓存
                try:
                    _refresh_daily_cache_for_kline_symbol(symbol)
                except Exception:
                    logging.exception("60 分钟 refresh 时顺带刷新日线缓存失败: %s", symbol)
                df = fetch_remote_60m()
            except Exception:  # noqa: BLE001
                logging.exception("拉取 %s 60 分钟数据失败，尝试回退本地缓存", symbol)
                if cached is None:
                    raise
                df = cached
                logging.warning("已回退使用本地 60m 缓存: %s (rows=%s)", symbol, len(df))

        if df is None or df.empty:
            raise ValueError(f"未获取到 {symbol} 的60分钟数据")

        rename_map = {
            "时间": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
        df = df.rename(columns=rename_map)
        required_cols = {"date", "open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            raise ValueError("60分钟行情数据缺少必要字段")

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        _save_kline_60_cache(symbol, df)
        macd_part = _calc_macd(df["close"])
        df = pd.concat([df, macd_part], axis=1)
        boll_part = _calc_boll(df["close"], period=20, num_std=2.0)
        df = pd.concat([df, boll_part], axis=1)
    else:
        raise ValueError("period 仅支持 daily 或 60")

    if df.empty:
        raise ValueError("指定日期区间内没有指数K线数据")

    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        dt = row["date"]
        if isinstance(dt, pd.Timestamp):
            date_str = dt.strftime("%Y-%m-%d %H:%M") if period == "60" else dt.strftime("%Y-%m-%d")
        else:
            date_str = str(dt)
        item: Dict[str, Any] = {
            "date": date_str,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        if period in ("daily", "60"):
            item["macd"] = {
                "dif": float(row["dif"]) if pd.notna(row.get("dif")) else 0.0,
                "dea": float(row["dea"]) if pd.notna(row.get("dea")) else 0.0,
                "macd": float(row["macd"]) if pd.notna(row.get("macd")) else 0.0,
            }
            item["boll"] = {
                "upper": float(row["upper"]) if pd.notna(row.get("upper")) else None,
                "middle": float(row["middle"]) if pd.notna(row.get("middle")) else None,
                "lower": float(row["lower"]) if pd.notna(row.get("lower")) else None,
            }
        rows.append(item)

    fractals: List[Dict[str, Any]] = []
    pens: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    pens_effective: List[Dict[str, Any]] = []
    if period in ("daily", "60"):
        source_bars: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            source_bars.append(
                {
                    "date": row["date"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                },
            )
        standardized_bars = _merge_inclusive_bars(source_bars)
        fractals = _find_fractals_from_standardized(standardized_bars)
        raw_idx = _raw_date_index_map(rows)
        pens = _build_bi_from_fractals(fractals, raw_idx)
        segments = _build_segments_from_pens(pens)
        pens_effective = _normalize_effective_pens(pens)

    result: Dict[str, Any] = {
        "symbol": symbol,
        "start_date": start_ts.strftime("%Y-%m-%d"),
        "end_date": end_ts.strftime("%Y-%m-%d"),
        "period": period,
        "adjust": _kline_adjust_label(symbol),
        "data": rows,
        "fractals": fractals,
        "pens": pens,
        "segments": segments,
        "pens_effective": pens_effective,
    }
    if period in ("daily", "60"):
        last_close = float(rows[-1]["close"]) if rows else 0.0
        result["centrals"] = _build_centrals(pens_effective, last_close, rows, max_visible=3)
    else:
        result["centrals"] = []
    _kline_cache_set(cache_key, result, symbol=symbol, period=period)
    return result

