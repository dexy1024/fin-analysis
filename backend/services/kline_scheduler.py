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

import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, time as time_of_day
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from services.buy_sell_signals import compute_and_save_buy_sell_signals
from services.defense_radar import DEFENSE_RADAR_WATCHLIST, _load_watchlist_observation_symbols, compute_and_save_broken_symbols, run_defense_radar
from services.indicators import get_index_kline
from services.position_manager import check_stop_loss, get_holdings, sell_all

TZ_SH = ZoneInfo("Asia/Shanghai")

# 调度任务中可预期的业务异常（网络、数据、IO问题），捕获后记录日志即可，不应导致调度线程崩溃
_SCHEDULER_EXPECTED_EXCEPTIONS = (ValueError, OSError, TypeError, KeyError, RuntimeError)


def _daily_start_date() -> str:
    return (datetime.now(TZ_SH) - timedelta(days=380)).strftime("%Y-%m-%d")

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
_slot_lock = threading.Lock()
_scheduler_lock_fd: Optional[object] = None  # 文件锁fd，用于多worker去重

# 调度器状态（供外部查询和监控）
_last_heartbeat: float = 0.0
_next_scheduled_time: Optional[datetime] = None
_last_slot_time: Optional[datetime] = None
_slot_execution_count: int = 0
_STATUS_FILE = "/tmp/kline_scheduler_status.json"


def _write_status_file() -> None:
    """把当前调度器状态写入共享文件，供多worker读取。"""
    try:
        with open(_STATUS_FILE, "w") as f:
            json.dump(
                {
                    "alive": _worker_thread is not None and _worker_thread.is_alive(),
                    "heartbeat_ts": _last_heartbeat,
                    "next_scheduled": _next_scheduled_time.isoformat() if _next_scheduled_time else None,
                    "last_slot": _last_slot_time.isoformat() if _last_slot_time else None,
                    "slot_count": _slot_execution_count,
                },
                f,
            )
    except (OSError, TypeError):
        pass


def _read_status_file() -> dict:
    """从共享文件读取调度器状态；若文件不存在则返回默认值。"""
    try:
        with open(_STATUS_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {
            "alive": False,
            "healthy": False,
            "thread_name": None,
            "next_scheduled": None,
            "last_slot": None,
            "slot_count": 0,
            "heartbeat_age_sec": None,
        }

# SSE 广播回调函数（由 main.py 设置）
_sse_callback: Optional[Callable[[bool, str], None]] = None


def set_sse_callback(callback: Callable[[bool, str], None]) -> None:
    """设置 SSE 广播回调函数"""
    global _sse_callback
    _sse_callback = callback
    logging.info("kline_scheduler: SSE 广播回调已设置")


def _h60_start_date() -> str:
    return (datetime.now(TZ_SH) - timedelta(days=79)).strftime("%Y-%m-%d")


def _h15_start_date() -> str:
    return (datetime.now(TZ_SH) - timedelta(days=30)).strftime("%Y-%m-%d")


# 15分钟独立同步槽位：交易时间内每根15分钟K线结束后1分钟触发
# 上午 9:30-11:30 / 下午 13:00-15:00
_H15_SLOTS: tuple[tuple[int, int], ...] = (
    (9, 46), (10, 1), (10, 16), (10, 31), (10, 46), (11, 1), (11, 16), (11, 31),
    (13, 16), (13, 31), (13, 46), (14, 1), (14, 16), (14, 31), (14, 46), (15, 1),
)


def sync_symbol_list_for_kline() -> list[str]:
    """上证 + DEFENSE_RADAR_WATCHLIST + observation.json 中的标的（去重）。"""
    codes = ["sh000001"] + [code for code, _ in DEFENSE_RADAR_WATCHLIST]
    for code, _ in _load_watchlist_observation_symbols():
        if code not in codes:
            codes.append(code)
    return codes


def _sync_all_daily() -> None:
    """写回日线 CSV；随后任意 get_index_kline(..., daily, refresh=false) 会因 mtime 变化重算日线 ABC 中枢。"""
    for sym in sync_symbol_list_for_kline():
        try:
            get_index_kline(
                symbol=sym,
                start_date=_daily_start_date(),
                end_date=None,
                period="daily",
                refresh=True,
            )
        except _SCHEDULER_EXPECTED_EXCEPTIONS:
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
        except _SCHEDULER_EXPECTED_EXCEPTIONS:
            logging.exception("kline_scheduler: 60m 同步失败 %s", sym)


def _sync_all_15m() -> None:
    """写回 kline_15_*.csv；随后任意 get_index_kline(..., 15, refresh=false) 会因 mtime 变化重算 15m ABC 中枢。"""
    h15 = _h15_start_date()
    for sym in sync_symbol_list_for_kline():
        try:
            get_index_kline(
                symbol=sym,
                start_date=h15,
                end_date=None,
                period="15",
                refresh=True,
            )
        except _SCHEDULER_EXPECTED_EXCEPTIONS:
            logging.exception("kline_scheduler: 15m 同步失败 %s", sym)


def _check_positions_stop_loss() -> None:
    """检查所有持仓的止损，触发时自动清仓"""
    holdings = get_holdings()
    if not holdings:
        return
    logging.info("kline_scheduler: 检查 %d 个持仓的止损", len(holdings))
    for pos in holdings:
        try:
            result = get_index_kline(
                symbol=pos.code,
                start_date=_h60_start_date(),
                end_date=None,
                period="60",
                refresh=False,
            )
            bars = result.get("data", [])
            if not bars:
                continue
            last_price = bars[-1].get("close")
            if last_price is None:
                continue

            stop_result = check_stop_loss(pos.code, float(last_price))
            if stop_result and stop_result["triggered"]:
                sell_all(pos.code, float(last_price), stop_result["reason"])
                logging.warning(
                    "kline_scheduler: 止损触发 %s %s @ %.2f, 原因: %s",
                    pos.code, pos.name, last_price, stop_result["reason"]
                )
        except _SCHEDULER_EXPECTED_EXCEPTIONS:
            logging.exception("kline_scheduler: 止损检查失败 %s", pos.code)


def run_scheduled_slot(include_daily: bool) -> None:
    """单次槽位任务：可选全量日线同步 → 全量 60m → 全量 15m → 持仓止损检查 → 双防线雷达 → 买卖信号。"""
    timestamp = datetime.now(TZ_SH).isoformat()
    logging.info("kline_scheduler: 槽位开始 include_daily=%s", include_daily)
    if include_daily:
        _sync_all_daily()
    _sync_all_60m()
    _sync_all_15m()
    # 检查持仓止损
    _check_positions_stop_loss()
    try:
        path = run_defense_radar(refresh=False)
        logging.info("kline_scheduler: 双防线雷达已写入 %s", path)
    except _SCHEDULER_EXPECTED_EXCEPTIONS:
        logging.exception("kline_scheduler: 双防线雷达失败")

    # 计算 watchlist + observation 的破位状态
    try:
        broken_path = compute_and_save_broken_symbols()
        logging.info("kline_scheduler: 破位状态已写入 %s", broken_path)
    except _SCHEDULER_EXPECTED_EXCEPTIONS:
        logging.exception("kline_scheduler: 破位状态计算失败")

    # 计算 watchlist + observation 的买卖信号
    try:
        buy_sell_path = compute_and_save_buy_sell_signals()
        logging.info("kline_scheduler: 买卖信号已写入 %s", buy_sell_path)
    except _SCHEDULER_EXPECTED_EXCEPTIONS:
        logging.exception("kline_scheduler: 买卖信号计算失败")

    # 调度完成后广播 SSE 消息
    try:
        if _sse_callback:
            _sse_callback(include_daily, timestamp)
            logging.info("kline_scheduler: SSE 广播已发送")
    except (OSError, TypeError, ValueError) as e:
        logging.warning("kline_scheduler: SSE 广播失败: %s", e)


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


def _next_h15_fire_after(now: datetime) -> datetime:
    """下一个15分钟独立同步触发时刻（严格晚于 now）。"""
    candidates: list[datetime] = []
    for day_i in range(14):
        day = (now + timedelta(days=day_i)).date()
        for hh, mm in _H15_SLOTS:
            t = datetime.combine(day, time_of_day(hour=hh, minute=mm, second=0), tzinfo=TZ_SH)
            if t > now:
                candidates.append(t)
    if not candidates:
        raise RuntimeError("kline_scheduler: 无法计算下一15分钟触发时刻")
    return min(candidates)


def _scheduler_worker_loop() -> None:
    """单次调度循环：计算下次时间（主槽位 vs 15m槽位取最近）→ 等待 → 执行。可被外层捕获异常后重启。"""
    global _next_scheduled_time
    slot_type = 'main'
    include_daily = False
    try:
        now = datetime.now(TZ_SH)
        main_when, main_include_daily = _next_fire_after(now)
        h15_when = _next_h15_fire_after(now)
        # 主槽位已包含15m同步，若时间相同或更早则优先执行主槽位（避免重复同步15m）
        if main_when <= h15_when:
            when = main_when
            slot_type = 'main'
            include_daily = main_include_daily
        else:
            when = h15_when
            slot_type = 'h15'
            include_daily = False
        _next_scheduled_time = when
        wait_sec = max(1.0, (when - now).total_seconds())
        logging.info(
            "kline_scheduler: 下次调度时间 %s (%.0f秒后), type=%s, include_daily=%s",
            when.isoformat(), wait_sec, slot_type, include_daily,
        )
    except (TypeError, ValueError, RuntimeError, OSError):
        logging.exception("kline_scheduler: 计算下一槽位失败，60s 后重试")
        if _stop_event.wait(timeout=60.0):
            return
        raise  # 抛到外层统一处理

    logging.info("kline_scheduler: 开始等待 %.0f 秒...", wait_sec)
    deadline = time.time() + wait_sec
    HEARTBEAT_INTERVAL = 1800.0  # 30分钟心跳
    next_heartbeat = time.time() + HEARTBEAT_INTERVAL

    while time.time() < deadline and not _stop_event.is_set():
        remaining = min(300.0, deadline - time.time())
        if remaining <= 0:
            break
        if _stop_event.wait(timeout=remaining):
            logging.info("kline_scheduler: 收到停止信号，退出等待")
            return

        # 心跳
        now_ts = time.time()
        if now_ts >= next_heartbeat:
            global _last_heartbeat
            _last_heartbeat = now_ts
            logging.info(
                "kline_scheduler: 心跳(存活) 距离下次调度还有 %.0f秒, 下次=%s",
                deadline - now_ts, _next_scheduled_time.isoformat() if _next_scheduled_time else "N/A",
            )
            _write_status_file()
            next_heartbeat = now_ts + HEARTBEAT_INTERVAL

    if _stop_event.is_set():
        return

    logging.info("kline_scheduler: 等待结束，开始执行槽位任务")
    try:
        if slot_type == 'main':
            run_scheduled_slot(include_daily)
            # 主槽位也触发状态机快照，确保15分钟CSV不漏
            try:
                from services.trade_command_engine import run_trade_command_engine
                run_trade_command_engine(generate_report=False)
                logging.info("kline_scheduler: 主槽位状态机快照已写入 CSV")
            except Exception:
                logging.exception("kline_scheduler: 主槽位状态机快照失败")
        else:
            _sync_all_15m()
            logging.info("kline_scheduler: 15m 独立同步完成")
            # 15分钟独立槽位：触发状态机快照（不写 Markdown 报告）
            try:
                from services.trade_command_engine import run_trade_command_engine
                run_trade_command_engine(generate_report=False)
                logging.info("kline_scheduler: 15m 状态机快照已写入 CSV")
            except Exception:
                logging.exception("kline_scheduler: 15m 状态机快照失败")
            # 14:46 槽位发送邮件通知（收盘前最后一根15分钟K线结束后）
            try:
                now_hm = datetime.now(TZ_SH)
                if now_hm.hour == 14 and now_hm.minute == 46:
                    from services.email_notifier import send_snapshot_alert
                    send_snapshot_alert(slot_time=now_hm)
            except Exception:
                logging.exception("kline_scheduler: 邮件通知发送失败")
        global _last_slot_time, _slot_execution_count
        with _slot_lock:
            _last_slot_time = datetime.now(TZ_SH)
            _slot_execution_count += 1
        logging.info("kline_scheduler: 槽位任务执行完成，继续下一次循环")
    except _SCHEDULER_EXPECTED_EXCEPTIONS:
        logging.exception("kline_scheduler: 槽位执行失败")


def _scheduler_worker() -> None:
    """调度线程入口：外层包裹致命异常捕获，确保线程不死。"""
    logging.info("kline_scheduler: 工作线程已启动")
    while not _stop_event.is_set():
        try:
            _scheduler_worker_loop()
        except Exception:
            # 此处捕获所有异常是调度线程的保活机制：任何未预料到的异常都不应杀死后台调度线程
            logging.exception("kline_scheduler: 工作线程遭遇致命异常，5秒后重启循环")
            global _last_heartbeat
            _last_heartbeat = time.time()
            if _stop_event.wait(timeout=5.0):
                break
    logging.info("kline_scheduler: 工作线程已退出")


def _check_and_run_missed_slot() -> None:
    """启动时检测：若距离上一个槽位不到30分钟，说明可能错过了，立即补跑一次60m任务。"""
    now = datetime.now(TZ_SH)
    # 收集过去24小时内所有槽位
    candidates: list[tuple[datetime, bool]] = []
    for day_i in range(-1, 1):
        day = (now + timedelta(days=day_i)).date()
        for hh, mm, inc in _KLINE_SLOTS:
            t = datetime.combine(day, time_of_day(hour=hh, minute=mm, second=0), tzinfo=TZ_SH)
            if t < now:
                candidates.append((t, inc))
    if not candidates:
        return
    # 最近的一个已过去槽位
    last_slot, last_inc = max(candidates, key=lambda x: x[0])
    elapsed = (now - last_slot).total_seconds()
    # 若距离上一个槽位在30分钟内（说明启动晚了或错过了），补跑
    if elapsed <= 1800:
        logging.warning(
            "kline_scheduler: 检测到可能错过槽位 %s (已过去%.0f秒)，立即补跑",
            last_slot.isoformat(), elapsed,
        )
        try:
            run_scheduled_slot(last_inc)
            global _last_slot_time, _slot_execution_count
            with _slot_lock:
                _last_slot_time = datetime.now(TZ_SH)
                _slot_execution_count += 1
            logging.info("kline_scheduler: 补跑槽位完成")
        except _SCHEDULER_EXPECTED_EXCEPTIONS:
            logging.exception("kline_scheduler: 补跑槽位失败")
    else:
        logging.info("kline_scheduler: 上一个槽位 %s 已过去%.0f秒，无需补跑", last_slot.isoformat(), elapsed)


def get_scheduler_status() -> dict:
    """供外部查询调度器健康状态。多worker环境下优先读共享状态文件。"""
    global _worker_thread
    alive = _worker_thread is not None and _worker_thread.is_alive()
    now_ts = time.time()
    heartbeat_age = now_ts - _last_heartbeat if _last_heartbeat else None

    # 如果本进程没有调度线程，尝试从共享状态文件读取
    if not alive:
        file_status = _read_status_file()
        if file_status.get("alive"):
            file_heartbeat = file_status.get("heartbeat_ts", 0)
            file_age = now_ts - file_heartbeat if file_heartbeat else float("inf")
            return {
                "alive": True,
                "healthy": file_age < 600,
                "thread_name": "kline-scheduler (其他worker)",
                "next_scheduled": file_status.get("next_scheduled"),
                "last_slot": file_status.get("last_slot"),
                "slot_count": file_status.get("slot_count", 0),
                "heartbeat_age_sec": int(file_age) if file_heartbeat else None,
            }

    # 心跳超过10分钟认为异常
    healthy = alive and (heartbeat_age is None or heartbeat_age < 600)
    status = {
        "alive": alive,
        "healthy": healthy,
        "thread_name": _worker_thread.name if _worker_thread else None,
        "next_scheduled": _next_scheduled_time.isoformat() if _next_scheduled_time else None,
        "last_slot": _last_slot_time.isoformat() if _last_slot_time else None,
        "slot_count": _slot_execution_count,
        "heartbeat_age_sec": int(heartbeat_age) if heartbeat_age is not None else None,
    }
    _write_status_file()
    return status


def setup_kline_scheduler() -> None:
    """启动后台守护线程；可重复调用时若已在跑则忽略。
    多worker环境下通过文件锁确保只有一个进程启动调度器。"""
    global _worker_thread, _last_heartbeat, _scheduler_lock_fd
    if _worker_thread is not None and _worker_thread.is_alive():
        logging.warning("kline_scheduler: 已在运行，跳过重复启动")
        return

    # 多worker去重：尝试获取文件锁，非阻塞
    lock_path = "/tmp/kline_scheduler.lock"
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()) + "\n")
        fd.flush()
        _scheduler_lock_fd = fd
        logging.info("kline_scheduler: 获取文件锁成功 (pid=%s)", os.getpid())
    except (IOError, OSError) as e:
        logging.info("kline_scheduler: 其他进程已持有锁，本进程跳过调度器启动 (%s)", e)
        return

    _stop_event.clear()
    _last_heartbeat = time.time()

    # 启动前检查是否需要补跑
    _check_and_run_missed_slot()

    _worker_thread = threading.Thread(
        target=_scheduler_worker,
        name="kline-scheduler",
        daemon=True,
    )
    _worker_thread.start()
    logging.info(
        "kline_scheduler: 已启动（15m: 交易时间每15分钟独立同步; "
        "主槽位: 10:31/11:31/14:01/15:01 60m+雷达; 16:01 日线+60m+雷达）",
    )


def shutdown_kline_scheduler() -> None:
    global _scheduler_lock_fd
    _stop_event.set()
    t = _worker_thread
    if t is not None and t.is_alive():
        t.join(timeout=8.0)
    # 释放多 worker 去重文件锁，避免 fd 泄漏
    if _scheduler_lock_fd is not None:
        try:
            _scheduler_lock_fd.close()
        except OSError:
            pass
        _scheduler_lock_fd = None
