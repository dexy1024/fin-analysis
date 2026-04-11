"""
后台定时任务（北京时间 Asia/Shanghai）：不依赖浏览器。
独立线程睡眠到下一槽位唤醒执行。
- 10:31 / 11:31 / 14:01 / 15:01：全量 60m refresh + 双防线雷达 CSV
- 16:01：全量日线 refresh + 上述 60m + 雷达

与 indicators.get_index_kline 响应缓存的关系（见该函数文档）：
- 日线本地 CSV（index_daily_*.csv / a_daily_*.csv）更新后，仅会使「period=daily」的内存缓存失效并重算
  该标的日线的分型/笔/线段/ABC 中枢；不影响 60 分钟缓存。
- 60 分钟本地 CSV（data/kline_60_*.csv）更新后，仅会使「period=60」的缓存失效并重算 60m 侧缠论与中枢；
  不影响日线缓存。
- 本模块在槽位内显式 refresh=True 会拉网写盘并触发上述重算；前端 refresh=false 时若检测到对应 CSV
  的 mtime 新于缓存记录，也会自动 purge 并重算，无需手工对齐。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, time as time_of_day
from typing import Optional
from zoneinfo import ZoneInfo

from services.defense_radar import DEFENSE_RADAR_WATCHLIST, run_defense_radar
from services.indicators import get_index_kline

TZ_SH = ZoneInfo("Asia/Shanghai")
DAILY_START = "2024-12-01"

# (hour, minute, include_daily)
_KLINE_SLOTS: tuple[tuple[int, int, bool], ...] = (
    (10, 31, False),
    (11, 31, False),
    (14, 1, False),
    (15, 1, False),
    (16, 1, True),
)

_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None


def _h60_start_date() -> str:
    return (datetime.now(TZ_SH) - timedelta(days=90)).strftime("%Y-%m-%d")


def sync_symbol_list_for_kline() -> list[str]:
    """与前端原 sync 列表一致：上证 + 全部 CHART 标的，不含港股小米（60m 数据源限制）。"""
    return ["sh000001"] + [
        code for code, _ in DEFENSE_RADAR_WATCHLIST if code.lower() != "hk01810"
    ]


def _sync_all_daily() -> None:
    """写回日线 CSV；随后任意 get_index_kline(..., daily, refresh=false) 会因 mtime 变化重算日线 ABC 中枢。"""
    for sym in sync_symbol_list_for_kline():
        try:
            get_index_kline(
                symbol=sym,
                start_date=DAILY_START,
                end_date=None,
                period="daily",
                refresh=True,
            )
        except Exception:  # noqa: BLE001
            logging.exception("kline_scheduler: 日线同步失败 %s", sym)


def _sync_all_60m() -> None:
    """写回 kline_60_*.csv；随后任意 get_index_kline(..., 60, refresh=false) 会因 mtime 变化重算 60m ABC 中枢。"""
    h60 = _h60_start_date()
    for sym in sync_symbol_list_for_kline():
        try:
            get_index_kline(
                symbol=sym,
                start_date=h60,
                end_date=None,
                period="60",
                refresh=True,
            )
        except Exception:  # noqa: BLE001
            logging.exception("kline_scheduler: 60m 同步失败 %s", sym)


def run_scheduled_slot(include_daily: bool) -> None:
    """单次槽位任务：可选全量日线同步 → 全量 60m → 双防线雷达（读本地，不写网）。"""
    logging.info("kline_scheduler: 槽位开始 include_daily=%s", include_daily)
    if include_daily:
        _sync_all_daily()
    _sync_all_60m()
    try:
        path = run_defense_radar(refresh=False)
        logging.info("kline_scheduler: 双防线雷达已写入 %s", path)
    except Exception:  # noqa: BLE001
        logging.exception("kline_scheduler: 双防线雷达失败")


def _next_fire_after(now: datetime) -> tuple[datetime, bool]:
    """下一个触发时刻（严格晚于 now）与是否带日线同步。"""
    candidates: list[tuple[datetime, bool]] = []
    for day_i in range(14):
        day = (now + timedelta(days=day_i)).date()
        for hh, mm, inc in _KLINE_SLOTS:
            t = datetime.combine(day, time_of_day(hour=hh, minute=mm, second=0), tzinfo=TZ_SH)
            if t > now:
                candidates.append((t, inc))
    if not candidates:
        raise RuntimeError("kline_scheduler: 无法计算下一触发时刻")
    return min(candidates, key=lambda x: x[0])


def _scheduler_worker() -> None:
    while not _stop_event.is_set():
        try:
            now = datetime.now(TZ_SH)
            when, include_daily = _next_fire_after(now)
            wait_sec = max(1.0, (when - datetime.now(TZ_SH)).total_seconds())
        except Exception:  # noqa: BLE001
            logging.exception("kline_scheduler: 计算下一槽位失败，60s 后重试")
            if _stop_event.wait(timeout=60.0):
                break
            continue
        logging.debug("kline_scheduler: 睡眠 %.0fs 至 %s", wait_sec, when.isoformat())
        if _stop_event.wait(timeout=wait_sec):
            break
        try:
            run_scheduled_slot(include_daily)
        except Exception:  # noqa: BLE001
            logging.exception("kline_scheduler: 槽位执行失败")


def setup_kline_scheduler() -> None:
    """启动后台守护线程；可重复调用时若已在跑则忽略。"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        logging.warning("kline_scheduler: 已在运行，跳过重复启动")
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_scheduler_worker,
        name="kline-scheduler",
        daemon=True,
    )
    _worker_thread.start()
    logging.info(
        "kline_scheduler: 已启动（10:31/11:31/14:01/15:01 60m+雷达；16:01 日线+60m+雷达）",
    )


def shutdown_kline_scheduler() -> None:
    _stop_event.set()
    t = _worker_thread
    if t is not None and t.is_alive():
        t.join(timeout=8.0)
