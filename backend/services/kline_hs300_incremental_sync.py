"""
K 线增量同步（HS300 / kline_scheduler / watchlist+observation 共用）。

- 日线：按本地日线 CSV 最后一根交易日为起点；A 股/ETF/指数新浪合并写回；港股 yfinance 优先写回 hk_daily_*.csv。
- 60m/15m：读 kline_{60|15}_*.csv 最后一根为起点；无有效缓存时 60m=79 日、15m=25 日。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as time_of_day
from pathlib import Path
from typing import Iterable, Literal, Optional

import pandas as pd
from zoneinfo import ZoneInfo

from services.index_cache import (
    _a_share_daily_cache_path,
    _cache_path,
    _hk_daily_cache_path,
    load_hk_daily_dataframe,
    load_index_daily_dataframe,
    sync_a_share_daily_cache_merged,
)
from services.indicators import (
    _kline_15_cache_path,
    _kline_60_cache_path,
    _split_kline_symbol,
    get_index_kline,
)
from services.kline_minute_sync import sync_minute_kline_to_csv
from services.trade_command_engine import _load_hs300_symbols
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

TZ_SH = ZoneInfo("Asia/Shanghai")
Period = Literal["daily", "60", "15"]
MinutePeriod = Literal["60", "15"]

# 与 kline_scheduler 槽位对齐：槽位触发后对应 60m/15m K 线应已落盘
_60M_AVAIL_AFTER: tuple[tuple[int, int, int, int], ...] = (
    (10, 31, 10, 30),
    (11, 31, 11, 30),
    (14, 1, 14, 0),
    (15, 1, 15, 0),
)
_15M_AVAIL_AFTER: tuple[tuple[int, int, int, int], ...] = tuple(
    (
        trig_h,
        trig_m,
        (trig_h * 60 + trig_m - 1) // 60,
        (trig_h * 60 + trig_m - 1) % 60,
    )
    for trig_h, trig_m in (
        (9, 46),
        (10, 1),
        (10, 16),
        (10, 31),
        (10, 46),
        (11, 1),
        (11, 16),
        (11, 31),
        (13, 16),
        (13, 31),
        (13, 46),
        (14, 1),
        (14, 16),
        (14, 31),
        (14, 46),
        (15, 1),
    )
)


def _is_hk_symbol(code: str) -> bool:
    c = code.strip().lower()
    return c.startswith("hk") or (c.isdigit() and len(c) == 5)


def _expected_last_minute_bar(now: datetime, period: MinutePeriod) -> pd.Timestamp | None:
    """
    返回当前时刻 A 股/ETF 应已可用的最后一根分钟 K 线时间戳（上海时区）。
    非交易日或当日首根 60m 尚未到点时返回 None（仍用「末根日期 < target_date」判定）。
    """
    if now.weekday() >= 5:
        return None
    slots = _60M_AVAIL_AFTER if period == "60" else _15M_AVAIL_AFTER
    t = now.timetz() if now.tzinfo else now.time()
    bar_h, bar_m = None, None
    for avail_h, avail_m, bh, bm in slots:
        if t >= time_of_day(avail_h, avail_m):
            bar_h, bar_m = bh, bm
    if bar_h is None:
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ts = day.replace(hour=bar_h, minute=bar_m, second=0, microsecond=0)
    return pd.Timestamp(ts.replace(tzinfo=None))


def _minute_bar_needs_sync(
    code: str,
    period: MinutePeriod,
    target_date: str,
    *,
    now: datetime | None = None,
) -> bool:
    """分钟 K 是否落后于 target_date 或当日应收的最后一根。"""
    last = last_bar_timestamp(code, period)
    if last is None:
        return True
    last_date = last.strftime("%Y-%m-%d")
    if last_date < target_date:
        return True
    if last_date > target_date:
        return False
    if _is_hk_symbol(code):
        return False
    now_sh = now or datetime.now(TZ_SH)
    expected = _expected_last_minute_bar(now_sh, period)
    if expected is None or expected.strftime("%Y-%m-%d") < target_date:
        return False
    return last < expected


def _daily_start_date() -> str:
    """与 kline_scheduler._daily_start_date 一致（无本地日线文件时的冷启动起点）。"""
    return (datetime.now(TZ_SH) - timedelta(days=380)).strftime("%Y-%m-%d")


def incremental_daily_start_date(code: str) -> str:
    """本地日线 CSV 最后一根交易日；无文件或读失败则 380 自然日冷启动。"""
    path = _resolve_daily_cache_path(code)
    if not path.is_file():
        return _daily_start_date()
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except EXPECTED_BUSINESS_EXCEPTIONS:
        return _daily_start_date()
    except Exception:
        raise
    if df.empty or "date" not in df.columns:
        return _daily_start_date()
    req = {"date", "open", "high", "low", "close", "volume"}
    if not req.issubset(df.columns):
        return _daily_start_date()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.dropna(subset=["date"])
    if df.empty:
        return _daily_start_date()
    last = df["date"].max()
    if pd.isna(last):
        return _daily_start_date()
    return last.strftime("%Y-%m-%d")


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
    except EXPECTED_BUSINESS_EXCEPTIONS:
        return None
    except Exception:
        raise
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


def _resolve_daily_cache_path(code: str) -> Path:
    c = code.strip().lower()
    if c.startswith("hk"):
        return _hk_daily_cache_path(code)
    if (c.startswith("sh") or c.startswith("sz")) and len(c) > 8:
        return _cache_path(code)
    return _a_share_daily_cache_path(code)


def _last_daily_bar_date(code: str) -> Optional[str]:
    path = _resolve_daily_cache_path(code)
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except EXPECTED_BUSINESS_EXCEPTIONS:
        return None
    except Exception:
        raise
    if df.empty or "date" not in df.columns:
        return None
    last = pd.to_datetime(df["date"]).max()
    return last.strftime("%Y-%m-%d") if pd.notna(last) else None


def _last_minute_bar_date(symbol: str, period: MinutePeriod) -> Optional[str]:
    last = last_bar_timestamp(symbol, period)
    return last.strftime("%Y-%m-%d") if last is not None else None


@dataclass
class SymbolFreshness:
    code: str
    name: str
    daily_last: Optional[str] = None
    m60_last: Optional[str] = None
    m15_last: Optional[str] = None
    needs_daily: bool = False
    needs_60: bool = False
    needs_15: bool = False

    @property
    def needs_any(self) -> bool:
        return self.needs_daily or self.needs_60 or self.needs_15

    @property
    def periods_to_sync(self) -> tuple[Period, ...]:
        out: list[Period] = []
        if self.needs_daily:
            out.append("daily")
        if self.needs_60:
            out.append("60")
        if self.needs_15:
            out.append("15")
        return tuple(out)

    def missing_labels(self) -> str:
        parts: list[str] = []
        if self.needs_daily:
            parts.append("daily")
        if self.needs_60:
            parts.append("60m")
        if self.needs_15:
            parts.append("15m")
        return "+".join(parts)


@dataclass
class StaleAuditReport:
    target_date: str
    total: int
    fresh: int
    stale: list[SymbolFreshness] = field(default_factory=list)
    need_daily: int = 0
    need_60: int = 0
    need_15: int = 0


def _check_symbol_freshness(
    code: str,
    name: str,
    target_date: str,
    *,
    now: datetime | None = None,
) -> SymbolFreshness:
    daily_last = _last_daily_bar_date(code)
    m60_last = _last_minute_bar_date(code, "60")
    m15_last = _last_minute_bar_date(code, "15")
    now_sh = now or datetime.now(TZ_SH)
    return SymbolFreshness(
        code=code,
        name=name,
        daily_last=daily_last,
        m60_last=m60_last,
        m15_last=m15_last,
        needs_daily=(daily_last or "") < target_date,
        needs_60=_minute_bar_needs_sync(code, "60", target_date, now=now_sh),
        needs_15=_minute_bar_needs_sync(code, "15", target_date, now=now_sh),
    )


def audit_kline_freshness(
    pairs: list[tuple[str, str]],
    target_date: Optional[str] = None,
) -> StaleAuditReport:
    """扫描给定标的列表，找出相对 target_date 未齐 daily/60m/15m 的项。"""
    target = target_date or datetime.now(TZ_SH).strftime("%Y-%m-%d")
    stale: list[SymbolFreshness] = []
    need_daily = need_60 = need_15 = 0
    for code, name in pairs:
        sf = _check_symbol_freshness(code, name, target)
        if sf.needs_any:
            stale.append(sf)
            if sf.needs_daily:
                need_daily += 1
            if sf.needs_60:
                need_60 += 1
            if sf.needs_15:
                need_15 += 1
    return StaleAuditReport(
        target_date=target,
        total=len(pairs),
        fresh=len(pairs) - len(stale),
        stale=stale,
        need_daily=need_daily,
        need_60=need_60,
        need_15=need_15,
    )


def audit_hs300_kline_freshness(target_date: Optional[str] = None) -> StaleAuditReport:
    """扫描 watchlist_hs300.json，找出相对 target_date 未齐 daily/60m/15m 的标的。"""
    return audit_kline_freshness(_load_hs300_symbols(), target_date)


def _accumulate_sync_result(
    summary: BatchSummary,
    result: SymbolSyncResult,
    period_list: tuple[Period, ...],
) -> None:
    if "daily" in period_list and result.touched_daily:
        if result.error_daily is None:
            summary.ok_daily += 1
        else:
            summary.fail_daily += 1
    if "60" in period_list and result.touched_60:
        if result.error_60 is None:
            summary.ok_60 += 1
        else:
            summary.fail_60 += 1
    if "15" in period_list and result.touched_15:
        if result.error_15 is None:
            summary.ok_15 += 1
        else:
            summary.fail_15 += 1


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
    period_sleep_sec: float = 0.0,
) -> SymbolSyncResult:
    period_list = tuple(periods)
    daily_start = incremental_daily_start_date(code) if "daily" in period_list else ""
    s60 = incremental_start_date(code, "60") if "60" in period_list else ""
    s15 = incremental_start_date(code, "15") if "15" in period_list else ""
    res = SymbolSyncResult(
        code=code,
        name=name,
        start_daily=daily_start,
        start_60=s60,
        start_15=s15,
        dry_run=dry_run,
    )
    if dry_run:
        return res
    for p in period_list:
        try:
            if p == "daily":
                api_sym, src = _split_kline_symbol(code)
                if src == "a_share":
                    sync_a_share_daily_cache_merged(code)
                elif src == "index":
                    load_index_daily_dataframe(api_sym, force_refresh=True)
                elif src == "hk":
                    load_hk_daily_dataframe(code, force_refresh=True)
                else:
                    raise ValueError(f"不支持标的: {code}")
                payload = get_index_kline(
                    symbol=code,
                    start_date=daily_start,
                    end_date=None,
                    period="daily",
                    refresh=False,
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
            if period_sleep_sec > 0 and p != period_list[-1]:
                time.sleep(period_sleep_sec)
        except EXPECTED_BUSINESS_EXCEPTIONS as exc:
            logging.warning("kline_hs300_incremental: %s %s 失败: %s", code, p, exc, exc_info=True)
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
        except Exception:
            logging.exception("kline_hs300_incremental: %s %s 未预期异常", code, p)
            raise
    return res


def sync_codes_incremental(
    codes: Iterable[str],
    periods: tuple[Period, ...],
    *,
    code_to_name: Optional[dict[str, str]] = None,
    sleep_sec: float = 0.0,
    log_label: str = "kline_incremental",
) -> None:
    """对给定 code 列表按周期做增量同步（kline_scheduler / 脚本共用）。"""
    period_list = tuple(dict.fromkeys(periods))
    names = code_to_name or {}
    for code in codes:
        sym = str(code).strip()
        if not sym:
            continue
        _sync_symbol(sym, names.get(sym, ""), period_list, dry_run=False)
        if sleep_sec > 0:
            time.sleep(sleep_sec)


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
    :param dry_run: 为 True 时不拉网，只计算并返回各标的的增量起点（日线为本地最后一交易日或冷启动日）
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
        _accumulate_sync_result(summary, r, period_list)
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    if not dry_run:
        _log_incremental_failures(summary, period_list)

    return summary


def run_kline_stale_repair(
    pairs: list[tuple[str, str]],
    *,
    label: str = "K线",
    target_date: Optional[str] = None,
    sleep_sec: float = 0.2,
    period_sleep_sec: float = 0.0,
    sync_periods: Optional[tuple[Period, ...]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> tuple[StaleAuditReport, BatchSummary]:
    """
    仅对 audit 中未齐 target_date 的标的，按缺失周期从本地末根增量补跑。
    """
    report = audit_kline_freshness(pairs, target_date)
    stale_list = report.stale
    if limit is not None:
        stale_list = stale_list[: max(0, limit)]

    summary = BatchSummary(symbols=len(stale_list))
    all_periods: tuple[Period, ...] = ("daily", "60", "15")

    logging.info(
        "%s 增量补跑: target=%s 待补=%d/%d (缺 daily=%d 60m=%d 15m=%d) dry_run=%s",
        label,
        report.target_date,
        len(stale_list),
        report.total,
        report.need_daily,
        report.need_60,
        report.need_15,
        dry_run,
    )

    for sf in stale_list:
        periods = sf.periods_to_sync
        if sync_periods is not None:
            periods = tuple(p for p in periods if p in sync_periods)
        if not periods:
            continue
        if dry_run:
            logging.info(
                "dry-run %s %s 缺 %s | daily=%s 60m=%s 15m=%s",
                sf.code,
                sf.name or "-",
                sf.missing_labels(),
                sf.daily_last or "-",
                sf.m60_last or "-",
                sf.m15_last or "-",
            )
            summary.results.append(
                _sync_symbol(sf.code, sf.name, periods, dry_run=True, period_sleep_sec=period_sleep_sec)
            )
        else:
            r = _sync_symbol(
                sf.code, sf.name, periods, dry_run=False, period_sleep_sec=period_sleep_sec
            )
            summary.results.append(r)
            _accumulate_sync_result(summary, r, periods)
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    if not dry_run:
        _log_incremental_failures(summary, all_periods)

    return report, summary


def run_hs300_kline_stale_repair(
    *,
    target_date: Optional[str] = None,
    sleep_sec: float = 0.2,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> tuple[StaleAuditReport, BatchSummary]:
    """HS300：仅补未齐标的的缺失周期（增量）。"""
    return run_kline_stale_repair(
        _load_hs300_symbols(),
        label="HS300",
        target_date=target_date,
        sleep_sec=sleep_sec,
        limit=limit,
        dry_run=dry_run,
    )


def run_hs300_kline_sync_until_fresh(
    *,
    target_date: Optional[str] = None,
    sleep_sec: float = 1.0,
    max_rounds: int = 10,
    stale_only: bool = False,
    dry_run: bool = False,
    limit: Optional[int] = None,
    codes: Optional[list[str]] = None,
) -> tuple[StaleAuditReport, int]:
    """
    全量 incremental（可选）→ 扫描 → 仅补缺失周期 → 未齐则重试，直到全部最新或达 max_rounds。
    """
    target = target_date or datetime.now(TZ_SH).strftime("%Y-%m-%d")
    rounds = 0
    periods: tuple[Period, ...] = ("daily", "60", "15")

    if dry_run:
        report = audit_hs300_kline_freshness(target)
        logging.info(
            "dry-run 扫描: target=%s 已齐=%d/%d 待补=%d (缺 daily=%d 60m=%d 15m=%d)",
            report.target_date,
            report.fresh,
            report.total,
            len(report.stale),
            report.need_daily,
            report.need_60,
            report.need_15,
        )
        for sf in report.stale[:30]:
            logging.info(
                "  %s %s 缺 %s | daily=%s 60m=%s 15m=%s",
                sf.code,
                sf.name or "-",
                sf.missing_labels(),
                sf.daily_last or "-",
                sf.m60_last or "-",
                sf.m15_last or "-",
            )
        if len(report.stale) > 30:
            logging.info("  ... 其余 %d 条省略", len(report.stale) - 30)
        return report, 0

    if not stale_only:
        logging.info("HS300 第 1 轮: 全量同步 daily+60m+15m (sleep=%.1fs)", sleep_sec)
        run_hs300_kline_incremental(
            periods=periods,
            sleep_sec=sleep_sec,
            limit=limit,
            codes=codes,
            dry_run=False,
        )
        rounds += 1

    while rounds < max_rounds:
        report = audit_hs300_kline_freshness(target)
        if limit is not None:
            report.stale = report.stale[: max(0, limit)]
        if not report.stale:
            logging.info(
                "HS300 全部已齐: target=%s (%d/%d) 共 %d 轮",
                target,
                report.fresh,
                report.total,
                rounds,
            )
            return report, rounds

        logging.info(
            "HS300 第 %d 轮补跑: target=%s 待补=%d/%d (缺 daily=%d 60m=%d 15m=%d)",
            rounds + 1,
            target,
            len(report.stale),
            report.total,
            report.need_daily,
            report.need_60,
            report.need_15,
        )
        run_hs300_kline_stale_repair(
            target_date=target,
            sleep_sec=sleep_sec,
            limit=limit,
            dry_run=False,
        )
        rounds += 1
        if limit is not None:
            break

    report = audit_hs300_kline_freshness(target)
    if report.stale:
        logging.warning(
            "HS300 仍未全齐: target=%s 待补=%d/%d (已跑 %d 轮，可加 --max-rounds 或 --sleep)",
            target,
            len(report.stale),
            report.total,
            rounds,
        )
    return report, rounds


def _needs_any_for_periods(sf: SymbolFreshness, periods: tuple[Period, ...]) -> bool:
    if "daily" in periods and sf.needs_daily:
        return True
    if "60" in periods and sf.needs_60:
        return True
    if "15" in periods and sf.needs_15:
        return True
    return False


def audit_kline_freshness_for_periods(
    pairs: list[tuple[str, str]],
    periods: tuple[Period, ...],
    target_date: Optional[str] = None,
) -> StaleAuditReport:
    """扫描 freshness，但仅按指定周期统计/判定 stale。"""
    report = audit_kline_freshness(pairs, target_date)
    stale = [sf for sf in report.stale if _needs_any_for_periods(sf, periods)]
    need_daily = sum(1 for sf in stale if sf.needs_daily and "daily" in periods)
    need_60 = sum(1 for sf in stale if sf.needs_60 and "60" in periods)
    need_15 = sum(1 for sf in stale if sf.needs_15 and "15" in periods)
    return StaleAuditReport(
        target_date=report.target_date,
        total=report.total,
        fresh=report.total - len(stale),
        stale=stale,
        need_daily=need_daily,
        need_60=need_60,
        need_15=need_15,
    )


def etf_em_qfq_fallback(
    pairs: list[tuple[str, str]],
    sleep_sec: float,
    *,
    periods: tuple[Period, ...],
    target_date: Optional[str] = None,
) -> int:
    """白名单 ETF（东财 qfq）分钟线未齐时，东财分段补拉 60m/15m。"""
    from services.etf_em_qfq import etf_needs_em_qfq
    from services.kline_15_backfill_em import backfill_etf_min_em_to_csv

    minute_periods = [p for p in periods if p in ("60", "15")]
    if not minute_periods:
        return 0

    report = audit_kline_freshness_for_periods(pairs, periods, target_date)
    n = 0
    for sf in report.stale:
        if not etf_needs_em_qfq(sf.code):
            continue
        for mp in minute_periods:
            if mp == "60" and not sf.needs_60:
                continue
            if mp == "15" and not sf.needs_15:
                continue
            start = incremental_start_date(sf.code, mp)  # type: ignore[arg-type]
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            try:
                backfill_etf_min_em_to_csv(
                    sf.code,
                    start[:10],
                    None,
                    period=mp,
                    chunk_calendar_days=3,
                    sleep_sec=max(0.8, sleep_sec * 0.4),
                )
                logging.info("%s ETF %sm 东财 qfq 补拉完成", sf.code, mp)
                n += 1
            except EXPECTED_BUSINESS_EXCEPTIONS:
                logging.warning("%s ETF %sm 东财 qfq 补拉失败", sf.code, mp, exc_info=True)
            except Exception:
                logging.exception("%s ETF %sm 东财 qfq 补拉未预期异常", sf.code, mp)
                raise
    return n


# 兼容旧名
etf_15m_em_fallback = etf_em_qfq_fallback


def run_pairs_kline_sync_stable(
    pairs: list[tuple[str, str]],
    *,
    periods: tuple[Period, ...],
    sleep_sec: float = 3.5,
    period_sleep_sec: float = 2.0,
    max_rounds: int = 3,
    etf_em_fallback: bool = True,
    label: str = "kline_sync",
) -> StaleAuditReport:
    """
    稳定模式：仅补未齐标的，多轮重试；放慢节奏降低新浪 456 限流概率。
    返回最后一轮 audit 结果。
    """
    target = datetime.now(TZ_SH).strftime("%Y-%m-%d")
    period_list = tuple(dict.fromkeys(periods))
    logging.info(
        "%s 稳定同步: 标的=%d 周期=%s sleep=%.1fs 周期间隔=%.1fs 最多%d轮",
        label,
        len(pairs),
        ",".join(period_list),
        sleep_sec,
        period_sleep_sec,
        max_rounds,
    )

    report: StaleAuditReport | None = None
    for round_i in range(1, max(1, max_rounds) + 1):
        try:
            run_kline_stale_repair(
                pairs,
                label=label,
                target_date=target,
                sleep_sec=sleep_sec,
                period_sleep_sec=period_sleep_sec,
                sync_periods=period_list,
                dry_run=False,
            )
        except EXPECTED_BUSINESS_EXCEPTIONS:
            logging.warning("%s 第 %d 轮补跑异常", label, round_i, exc_info=True)
        except Exception:
            logging.exception("%s 第 %d 轮补跑未预期异常", label, round_i)
            raise

        if etf_em_fallback and ("15" in period_list or "60" in period_list):
            try:
                etf_em_qfq_fallback(pairs, sleep_sec, periods=period_list, target_date=target)
            except EXPECTED_BUSINESS_EXCEPTIONS:
                logging.warning("%s ETF 东财 qfq fallback 异常", label, exc_info=True)
            except Exception:
                logging.exception("%s ETF 东财 qfq fallback 未预期异常", label)
                raise

        report = audit_kline_freshness_for_periods(pairs, period_list, target)
        logging.info(
            "%s 第 %d 轮: 已齐 %d/%d，仍缺 %d (daily=%d 60m=%d 15m=%d)",
            label,
            round_i,
            report.fresh,
            report.total,
            len(report.stale),
            report.need_daily,
            report.need_60,
            report.need_15,
        )
        if not report.stale:
            logging.info("%s 全部标的已齐", label)
            return report

        if round_i < max_rounds:
            round_pause = max(sleep_sec * 2.5, 15.0)
            logging.info(
                "%s 第 %d 轮仍有 %d 个未齐，%.0fs 后再补跑（防新浪 456 限流）",
                label,
                round_i,
                len(report.stale),
                round_pause,
            )
            time.sleep(round_pause)

    assert report is not None
    if report.stale:
        logging.warning(
            "%s %d 轮后仍有 %d 个标的未齐（可加 sleep 或 max_rounds）",
            label,
            max_rounds,
            len(report.stale),
        )
        for sf in report.stale[:15]:
            logging.warning(
                "  %s %s 缺 %s | daily=%s 60m=%s 15m=%s",
                sf.code,
                sf.name or "-",
                sf.missing_labels(),
                sf.daily_last or "-",
                sf.m60_last or "-",
                sf.m15_last or "-",
            )
    return report


def _truncate_err(msg: str, max_len: int = 220) -> str:
    msg = msg.replace("\n", " ").strip()
    if len(msg) <= max_len:
        return msg
    return msg[: max_len - 3] + "..."


def _log_incremental_failures(summary: BatchSummary, period_list: tuple[Period, ...]) -> None:
    """跑完后一次性打出失败标的（周期 + code + 名 + 错误摘要）。"""
    lines: list[str] = []
    for r in summary.results:
        nm = (r.name or "").strip()
        label = f"{r.code} {nm}".strip() if nm else r.code
        if "daily" in period_list and r.touched_daily and r.error_daily:
            lines.append(f"daily\t{label}\t{_truncate_err(r.error_daily)}")
        if "60" in period_list and r.touched_60 and r.error_60:
            lines.append(f"60m\t{label}\t{_truncate_err(r.error_60)}")
        if "15" in period_list and r.touched_15 and r.error_15:
            lines.append(f"15m\t{label}\t{_truncate_err(r.error_15)}")
    if not lines:
        return
    logging.warning("HS300 同步失败明细（共 %d 条）:", len(lines))
    for line in lines:
        logging.warning("  %s", line)
