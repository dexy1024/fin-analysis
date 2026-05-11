"""
沪深300（watchlist_hs300.json）K 线：手动同步（可选日线 + 增量 60m/15m）。

- 日线：与 kline_scheduler 一致，get_index_kline(..., daily, refresh=True)，start_date 为最近 380 自然日。
- 60m/15m：读取本地 data/kline_{60|15}_{code}.csv 最后一根 date 为起点（与缓存重叠一根，merge 按 date keep last）。
- 无分钟缓存或伪缓存时：60m=79 自然日、15m=25 自然日冷启动窗口。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Literal, Optional

import pandas as pd
from zoneinfo import ZoneInfo

from services.indicators import _kline_15_cache_path, _kline_60_cache_path, get_index_kline
from services.kline_minute_sync import sync_minute_kline_to_csv
from services.trade_command_engine import _load_hs300_symbols

TZ_SH = ZoneInfo("Asia/Shanghai")
Period = Literal["daily", "60", "15"]
MinutePeriod = Literal["60", "15"]


def _daily_start_date() -> str:
    """与 kline_scheduler._daily_start_date 一致。"""
    return (datetime.now(TZ_SH) - timedelta(days=380)).strftime("%Y-%m-%d")


def _cold_start_start(period: MinutePeriod) -> str:
    now = datetime.now(TZ_SH)
    if period == "60":
        return (now - timedelta(days=79)).strftime("%Y-%m-%d")
    return (now - timedelta(days=25)).strftime("%Y-%m-%d")


def last_bar_timestamp(symbol: str, period: MinutePeriod) -> Optional[pd.Timestamp]:
    """本地 CSV 中最后一根 K 的时间；无有效缓存则 None。"""
    path = _kline_60_cache_path(symbol) if period == "60" else _kline_15_cache_path(symbol)
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except Exception:
        return None
    if df.empty or "date" not in df.columns:
        return None
    req = {"date", "open", "high", "low", "close", "volume"}
    if not req.issubset(df.columns):
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["date"])
    if df.empty:
        return None
    hm = set(df["date"].dt.strftime("%H:%M"))
    if hm == {"15:00"}:
        return None
    last = df["date"].max()
    return last if pd.notna(last) else None


def incremental_start_date(symbol: str, period: MinutePeriod) -> str:
    """供 sync_minute_kline_to_csv(..., start_date=...) 使用的起点字符串。"""
    last = last_bar_timestamp(symbol, period)
    if last is None:
        return _cold_start_start(period)
    return last.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SymbolSyncResult:
    code: str
    name: str
    start_daily: str = ""
    start_60: str = ""
    start_15: str = ""
    rows_daily: Optional[int] = None
    rows_60: Optional[int] = None
    rows_15: Optional[int] = None
    error_daily: Optional[str] = None
    error_60: Optional[str] = None
    error_15: Optional[str] = None
    touched_daily: bool = False
    touched_60: bool = False
    touched_15: bool = False
    dry_run: bool = False


@dataclass
class BatchSummary:
    symbols: int = 0
    ok_daily: int = 0
    ok_60: int = 0
    ok_15: int = 0
    fail_daily: int = 0
    fail_60: int = 0
    fail_15: int = 0
    results: list[SymbolSyncResult] = field(default_factory=list)


def _sync_symbol(
    code: str,
    name: str,
    periods: Iterable[Period],
    *,
    dry_run: bool,
) -> SymbolSyncResult:
    period_list = tuple(periods)
    sd = _daily_start_date() if "daily" in period_list else ""
    s60 = incremental_start_date(code, "60") if "60" in period_list else ""
    s15 = incremental_start_date(code, "15") if "15" in period_list else ""
    res = SymbolSyncResult(
        code=code,
        name=name,
        start_daily=sd,
        start_60=s60,
        start_15=s15,
        dry_run=dry_run,
    )
    if dry_run:
        return res
    for p in period_list:
        try:
            if p == "daily":
                payload = get_index_kline(
                    symbol=code,
                    start_date=sd,
                    end_date=None,
                    period="daily",
                    refresh=True,
                )
                res.rows_daily = len(payload.get("data", []))
                res.touched_daily = True
            else:
                start = incremental_start_date(code, p)
                n = sync_minute_kline_to_csv(code, p, start_date=start, end_date=None)  # type: ignore[arg-type]
                if p == "60":
                    res.rows_60 = n
                    res.touched_60 = True
                else:
                    res.rows_15 = n
                    res.touched_15 = True
        except Exception as exc:  # noqa: BLE001
            logging.exception("kline_hs300_incremental: %s %s 失败", code, p)
            msg = f"{type(exc).__name__}: {exc}"
            if p == "daily":
                res.error_daily = msg
                res.touched_daily = True
            elif p == "60":
                res.error_60 = msg
                res.touched_60 = True
            else:
                res.error_15 = msg
                res.touched_15 = True
    return res


def run_hs300_kline_incremental(
    *,
    periods: tuple[Period, ...] = ("60", "15"),
    sleep_sec: float = 0.0,
    limit: Optional[int] = None,
    codes: Optional[list[str]] = None,
    dry_run: bool = False,
) -> BatchSummary:
    """
    对 watchlist_hs300.json 中的标的做增量分钟 K 同步。

    :param periods: 同步周期子集
    :param sleep_sec: 每处理完一个标的（各周期）后的休眠，略降频
    :param limit: 仅处理前 N 个（调试用）
    :param codes: 若给出则只处理这些 code（仍须存在于 hs300 json 时可省略校验，直接按 code 拉）
    :param dry_run: 为 True 时不拉网，只计算并返回各标的的增量起点
    """
    if codes is not None:
        pairs = [(c.strip(), "") for c in codes if c.strip()]
    else:
        pairs = _load_hs300_symbols()
    if limit is not None:
        pairs = pairs[: max(0, limit)]

    summary = BatchSummary(symbols=len(pairs))
    period_list = tuple(dict.fromkeys(periods))  # 保序去重

    for code, name in pairs:
        if dry_run:
            r = _sync_symbol(code, name, period_list, dry_run=True)
            summary.results.append(r)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            continue
        r = _sync_symbol(code, name, period_list, dry_run=False)
        summary.results.append(r)
        if "daily" in period_list and r.touched_daily:
            if r.error_daily is None:
                summary.ok_daily += 1
            else:
                summary.fail_daily += 1
        if "60" in period_list and r.touched_60:
            if r.error_60 is None:
                summary.ok_60 += 1
            else:
                summary.fail_60 += 1
        if "15" in period_list and r.touched_15:
            if r.error_15 is None:
                summary.ok_15 += 1
            else:
                summary.fail_15 += 1
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    return summary
