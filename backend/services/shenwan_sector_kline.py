"""申万二级行业 K 线：日线走 AKShare 官方指数；60m/15m 由成分股分钟线等权合成。"""

from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import akshare as ak
import pandas as pd

from services.indicators import (
    _is_kline_cache_sufficient,
    _load_kline_15_cache,
    _load_kline_60_cache,
    _save_kline_15_cache,
    _save_kline_60_cache,
    build_kline_response_from_ohlc_df,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
SECTORS_JSON = ROOT_DIR / "shenwan_v2_sectors.json"
SECTOR_CODES_JSON = ROOT_DIR / "shenwan_v2_sector_codes.json"

MIN_CONSTITUENTS = 5
MAX_SYNC_CONSTITUENTS = 40
SYNC_SLEEP_SEC = 0.15

Period = Literal["daily", "60", "15"]


def _normalize_sector_name(name: str) -> str:
    return re.sub(r"\s+", "", name.strip())


def _to_a_share_symbol(code6: str) -> str:
    c = code6.strip()
    if c.startswith("6"):
        return f"sh{c}"
    return f"sz{c}"


def _normalize_constituent_code(raw: str) -> Optional[str]:
    s = raw.strip().lower()
    if s.startswith(("sh", "sz")):
        return s[2:]
    if re.fullmatch(r"\d{6}", s):
        return s
    return None


@lru_cache(maxsize=1)
def _sw_index_code_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    df = ak.sw_index_second_info()
    for _, row in df.iterrows():
        code = str(row["行业代码"]).strip()
        if code.endswith(".SI"):
            code = code[:-3]
        name = str(row["行业名称"]).strip()
        mapping[name] = code
        mapping[_normalize_sector_name(name)] = code
    return mapping


@lru_cache(maxsize=1)
def _sector_constituents_map() -> dict[str, list[str]]:
    if not SECTORS_JSON.is_file():
        logging.warning("shenwan_sector_kline: 未找到 %s", SECTORS_JSON)
        return {}
    try:
        rows = json.loads(SECTORS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("shenwan_sector_kline: 读取 sectors json 失败: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for row in rows:
        sector_code = str(row.get("sector_code") or "").strip()
        if not sector_code:
            continue
        codes: list[str] = []
        seen: set[str] = set()
        for st in row.get("stocks") or []:
            nc = _normalize_constituent_code(str(st.get("stock_code") or ""))
            if nc and nc not in seen:
                seen.add(nc)
                codes.append(nc)
        out[sector_code] = codes
    return out


def resolve_sw_index_code(sector_code: str, sector_name: str) -> Optional[str]:
    by_name = _sw_index_code_map().get(sector_name) or _sw_index_code_map().get(
        _normalize_sector_name(sector_name)
    )
    if by_name:
        return by_name
    m = re.fullmatch(r"sw2_(\d+)", sector_code.strip())
    if m:
        return m.group(1)
    return None


@lru_cache(maxsize=1)
def _sector_code_name_map() -> dict[str, str]:
    """sw2_* → 行业名称（shenwan_v2_sector_codes.json，缺则回退 shenwan_v2_sectors.json）。"""
    mapping: dict[str, str] = {}
    for path in (SECTOR_CODES_JSON, SECTORS_JSON):
        if not path.is_file():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("shenwan_sector_kline: 读取 %s 失败: %s", path.name, exc)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("sector_code") or row.get("code") or "").strip()
            name = str(row.get("sector_name") or row.get("name") or "").strip()
            if code and name and code not in mapping:
                mapping[code] = name
    return mapping


def lookup_shenwan_v2_sector_name(sector_code: str) -> str:
    return _sector_code_name_map().get(sector_code.strip(), "")


def load_shenwan_v2_sector_pairs() -> list[tuple[str, str]]:
    """全部申万二级 (code, name)，来自 shenwan_v2_sector_codes.json。"""
    pairs: list[tuple[str, str]] = []
    for code, name in _sector_code_name_map().items():
        if code.startswith("sw2_"):
            pairs.append((code, name))
    pairs.sort(key=lambda x: x[0])
    return pairs


def load_shenwan_v2_observation_pairs() -> list[tuple[str, str]]:
    """兼容旧名；等同 load_shenwan_v2_sector_pairs。"""
    return load_shenwan_v2_sector_pairs()


def constituent_codes(sector_code: str) -> list[str]:
    return list(_sector_constituents_map().get(sector_code.strip(), []))


def fetch_sw_sector_ohlc_df(sw_index_code: str, start_date: str) -> pd.DataFrame:
    raw = ak.index_hist_sw(symbol=sw_index_code, period="day")
    if raw is None or raw.empty:
        raise ValueError(f"申万指数 {sw_index_code} 无日线数据")
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["日期"], errors="coerce"),
            "open": pd.to_numeric(raw["开盘"], errors="coerce"),
            "high": pd.to_numeric(raw["最高"], errors="coerce"),
            "low": pd.to_numeric(raw["最低"], errors="coerce"),
            "close": pd.to_numeric(raw["收盘"], errors="coerce"),
            "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
        }
    )
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    start_ts = pd.to_datetime(start_date).normalize()
    df = df[df["date"] >= start_ts].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"申万指数 {sw_index_code} 在 {start_date} 之后无数据")
    df["volume"] = df["volume"].fillna(0.0)
    return df


def _minute_range(start_date: str, end_date: Optional[str], period: Period) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = pd.to_datetime(start_date)
    if end_date:
        end_ts = pd.to_datetime(end_date)
        if end_ts.normalize() == end_ts:
            end_ts = end_ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    else:
        end_ts = pd.Timestamp.now()
    if end_ts < start_ts:
        raise ValueError("end_date 不能早于 start_date")
    return start_ts, end_ts


def _load_constituent_minute_df(
    code6: str,
    period: Period,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> Optional[pd.DataFrame]:
    loader = _load_kline_60_cache if period == "60" else _load_kline_15_cache
    df = loader(code6, start_ts, end_ts)
    if df is None or df.empty:
        return None
    hm = set(df["date"].dt.strftime("%H:%M"))
    if hm == {"15:00"}:
        return None
    return df.reset_index(drop=True)


def _sync_constituent_minute(code6: str, period: Period, start_date: str) -> None:
    from services.kline_minute_sync import sync_minute_kline_to_csv

    sync_minute_kline_to_csv(_to_a_share_symbol(code6), period, start_date, None)  # type: ignore[arg-type]


def _collect_constituent_minute_dfs(
    sector_code: str,
    period: Period,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    *,
    start_date: str,
    sync_missing: bool,
) -> list[pd.DataFrame]:
    codes = constituent_codes(sector_code)
    if not codes:
        raise ValueError(f"行业 {sector_code} 无成分股列表（检查 shenwan_v2_sectors.json）")

    dfs: list[pd.DataFrame] = []
    missing: list[str] = []
    for code in codes:
        df = _load_constituent_minute_df(code, period, start_ts, end_ts)
        if df is not None and len(df) >= 12:
            dfs.append(df)
        else:
            missing.append(code)

    if sync_missing and missing and len(dfs) < max(MIN_CONSTITUENTS, len(codes) // 4):
        synced = 0
        for code in missing:
            if synced >= MAX_SYNC_CONSTITUENTS:
                break
            try:
                _sync_constituent_minute(code, period, start_date)
                synced += 1
                df = _load_constituent_minute_df(code, period, start_ts, end_ts)
                if df is not None and len(df) >= 12:
                    dfs.append(df)
            except Exception:
                logging.warning(
                    "shenwan_sector_kline: 成分股 %s %sm 同步失败",
                    code,
                    period,
                    exc_info=True,
                )
            time.sleep(SYNC_SLEEP_SEC)

    return dfs


def _aggregate_equal_weight_minute(dfs: list[pd.DataFrame]) -> pd.DataFrame:
    """多股等权合成行业分钟 OHLC（首根收盘价归一）。"""
    normed: list[pd.DataFrame] = []
    for df in dfs:
        base = float(df["close"].iloc[0])
        if base <= 0:
            continue
        part = df[["date", "open", "high", "low", "close", "volume"]].copy()
        for col in ("open", "high", "low", "close"):
            part[col] = part[col] / base
        normed.append(part.set_index("date"))

    if not normed:
        raise ValueError("成分股分钟线归一后为空")

    all_ts = sorted(set().union(*[set(n.index) for n in normed]))
    rows: list[dict[str, Any]] = []
    for ts in all_ts:
        o = h = l = c = vol = 0.0
        n = 0
        for nrm in normed:
            if ts not in nrm.index:
                continue
            row = nrm.loc[ts]
            o += float(row["open"])
            h += float(row["high"])
            l += float(row["low"])
            c += float(row["close"])
            vol += float(row["volume"])
            n += 1
        if n < 3:
            continue
        rows.append(
            {
                "date": ts,
                "open": o / n,
                "high": h / n,
                "low": l / n,
                "close": c / n,
                "volume": vol,
            }
        )

    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    if out.empty:
        raise ValueError("成分股分钟线合成结果为空")
    return out


def _scale_minute_to_official_close(agg: pd.DataFrame, official_close: float) -> pd.DataFrame:
    last = float(agg["close"].iloc[-1])
    if last <= 0 or official_close <= 0:
        return agg
    factor = official_close / last
    scaled = agg.copy()
    for col in ("open", "high", "low", "close"):
        scaled[col] = scaled[col] * factor
    return scaled


def fetch_sw_sector_minute_ohlc_df(
    sector_code: str,
    sector_name: str,
    *,
    period: Period,
    start_date: str,
    end_date: Optional[str] = None,
    sync_missing: bool = False,
) -> pd.DataFrame:
    if period not in ("60", "15"):
        raise ValueError(f"分钟合成仅支持 period=60/15，收到 {period!r}")

    sw_code = resolve_sw_index_code(sector_code, sector_name)
    if not sw_code:
        raise ValueError(f"无法解析申万指数代码: {sector_code} / {sector_name}")

    start_ts, end_ts = _minute_range(start_date, end_date, period)
    loader = _load_kline_60_cache if period == "60" else _load_kline_15_cache
    saver = _save_kline_60_cache if period == "60" else _save_kline_15_cache

    cached = loader(sector_code, start_ts, end_ts)
    if not sync_missing and _is_kline_cache_sufficient(cached, start_ts, end_ts):
        return cached.reset_index(drop=True)  # type: ignore[union-attr]

    dfs = _collect_constituent_minute_dfs(
        sector_code,
        period,
        start_ts,
        end_ts,
        start_date=start_date,
        sync_missing=sync_missing,
    )
    if len(dfs) < MIN_CONSTITUENTS:
        raise ValueError(
            f"行业 {sector_name}({sector_code}) {period}m 可用成分股不足 "
            f"({len(dfs)}<{MIN_CONSTITUENTS})，请先同步成分股分钟线或 refresh=true"
        )

    agg = _aggregate_equal_weight_minute(dfs)
    daily = fetch_sw_sector_ohlc_df(sw_code, start_date)
    agg = _scale_minute_to_official_close(agg, float(daily["close"].iloc[-1]))
    saver(sector_code, agg)
    logging.info(
        "shenwan_sector_kline: 合成 %s %sm 成分股=%d 输出=%d 根",
        sector_code,
        period,
        len(dfs),
        len(agg),
    )
    return agg


def get_sw_sector_kline_by_code(
    sector_code: str,
    *,
    start_date: str,
    period: str = "daily",
    end_date: Optional[str] = None,
    refresh: bool = False,
) -> dict[str, Any]:
    name = lookup_shenwan_v2_sector_name(sector_code)
    if not name:
        raise ValueError(f"未在 shenwan_v2_sector_codes.json 中找到行业: {sector_code}")
    return get_sw_sector_kline(
        sector_code,
        name,
        start_date=start_date,
        end_date=end_date,
        period=period,
        refresh=refresh,
    )


def get_sw_sector_kline(
    sector_code: str,
    sector_name: str,
    *,
    start_date: str,
    period: str = "daily",
    end_date: Optional[str] = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """sw2_* 行业 K 线，返回结构与 get_index_kline 一致。"""
    if period == "daily":
        sw_code = resolve_sw_index_code(sector_code, sector_name)
        if not sw_code:
            raise ValueError(f"无法解析申万指数代码: {sector_code} / {sector_name}")
        df = fetch_sw_sector_ohlc_df(sw_code, start_date)
        end = df["date"].iloc[-1].strftime("%Y-%m-%d")
        return build_kline_response_from_ohlc_df(
            sector_code,
            df,
            period="daily",
            start_date=start_date,
            end_date=end,
        )

    if period not in ("60", "15"):
        raise ValueError(f"不支持的 period: {period!r}")

    df = fetch_sw_sector_minute_ohlc_df(
        sector_code,
        sector_name,
        period=period,  # type: ignore[arg-type]
        start_date=start_date,
        end_date=end_date,
        sync_missing=refresh,
    )
    end = pd.to_datetime(df["date"].iloc[-1])
    end_str = end.strftime("%Y-%m-%d %H:%M") if period in ("60", "15") else end.strftime("%Y-%m-%d")
    return build_kline_response_from_ohlc_df(
        sector_code,
        df,
        period=period,
        start_date=start_date,
        end_date=end_str.split(" ")[0],
    )
