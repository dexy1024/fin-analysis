"""
步进式（Walk-Forward）回测引擎：按 15 分钟 K 线逐根推进，在时刻 T 仅用 T 及以前的数据
调用与实盘一致的 `get_index_kline(end_date=T)` + `_run_symbol_analysis_pipeline` + `build_snapshot_data`，
从快照字段「实际交易动作」得到买卖指令。不实现任何独立的买卖规则。

依赖本地/缓存 K 线（与作战引擎相同）；请先保证 `kline_15_*.csv` / 日线等覆盖回测区间。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# 路径：支持以「项目根」或「backend」为 cwd 调用
# ---------------------------------------------------------------------------
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from services.indicators import get_index_kline
from services.trade_command_engine import (
    INDEX_CODE,
    TZ_SH,
    _compute_market_state,
    _run_symbol_analysis_pipeline,
)
from utils.csv_logger import build_snapshot_data

INITIAL_CASH = 50_000.0


def _parse_ts(s: str) -> pd.Timestamp:
    ts = pd.to_datetime(s)
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(TZ_SH)
    else:
        ts = ts.tz_convert(TZ_SH)
    return ts


def _bar_index_end_str(t: pd.Timestamp) -> str:
    """传入带时区的 T，输出 get_index_kline 可用的 end_date 字符串（本地 naive 时分秒）。"""
    t_naive = t.tz_convert(TZ_SH).tz_localize(None)
    return t_naive.strftime("%Y-%m-%d %H:%M:%S")


def _window_starts(t: pd.Timestamp) -> Tuple[str, str, str]:
    """与作战引擎一致：日线 380d、60m 79d、15m 25d。"""
    t_naive = t.tz_convert(TZ_SH).tz_localize(None)
    daily_start = (t_naive - pd.Timedelta(days=380)).strftime("%Y-%m-%d")
    h60_start = (t_naive - pd.Timedelta(days=79)).strftime("%Y-%m-%d")
    h15_start = (t_naive - pd.Timedelta(days=25)).strftime("%Y-%m-%d")
    return daily_start, h60_start, h15_start


def _load_timeline_15m(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> List[pd.Timestamp]:
    """拉取一根完整 15m 序列仅用于生成推进时间轴（起止含端点）。"""
    daily_start = (start.tz_convert(TZ_SH).tz_localize(None) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    end_str = _bar_index_end_str(end)
    try:
        resp = get_index_kline(
            symbol=symbol,
            start_date=daily_start,
            end_date=end_str,
            period="15",
            refresh=False,
        )
    except Exception:
        logging.exception("walk_forward: 构建 15m 时间轴失败 %s", symbol)
        return []
    out: List[pd.Timestamp] = []
    for row in resp.get("data") or []:
        d = row.get("date")
        if not d:
            continue
        ts = pd.to_datetime(d)
        if getattr(ts, "tz", None) is not None:
            ts = ts.tz_convert(TZ_SH)
        else:
            ts = ts.tz_localize(TZ_SH)
        if ts < start or ts > end:
            continue
        out.append(ts)
    out.sort()
    return out


def _fetch_triple_at_t(
    symbol: str, t: pd.Timestamp
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    daily_s, h60_s, h15_s = _window_starts(t)
    end_s = _bar_index_end_str(t)
    daily_r, h60_r, h15_r = None, None, None
    try:
        daily_r = get_index_kline(symbol=symbol, start_date=daily_s, end_date=end_s, period="daily", refresh=False)
    except Exception:
        logging.debug("walk_forward: %s 日线 @ %s 失败", symbol, end_s, exc_info=True)
    try:
        h60_r = get_index_kline(symbol=symbol, start_date=h60_s, end_date=end_s, period="60", refresh=False)
    except Exception:
        logging.debug("walk_forward: %s 60m @ %s 失败", symbol, end_s, exc_info=True)
    try:
        h15_r = get_index_kline(symbol=symbol, start_date=h15_s, end_date=end_s, period="15", refresh=False)
    except Exception:
        logging.debug("walk_forward: %s 15m @ %s 失败", symbol, end_s, exc_info=True)
    return daily_r, h60_r, h15_r


def _fetch_index_triple_at_t(
    t: pd.Timestamp,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    return _fetch_triple_at_t(INDEX_CODE, t)


def _close_at_t(h15_result: Optional[Dict[str, Any]], t: pd.Timestamp) -> Optional[float]:
    """在已用 end_date=T 截断的 15m 响应中取收盘价（最后一根即为 T）。"""
    if not h15_result or not h15_result.get("data"):
        return None
    try:
        last = h15_result["data"][-1]
        d_raw = str(last.get("date", ""))
        end_s = _bar_index_end_str(t)
        if d_raw[:16] != end_s[:16]:
            logging.debug("walk_forward: 15m 最后一根日期 %s 与轴 %s 不一致", d_raw, end_s)
        return float(last["close"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None


@dataclass
class WalkForwardResult:
    symbol: str
    name: str
    start: str
    end: str
    initial_cash: float
    final_equity: float
    max_drawdown_pct: float
    trades: List[Dict[str, Any]] = field(default_factory=list)
    equity_series: List[Tuple[str, float]] = field(default_factory=list)


def run_walk_forward_backtest(
    symbol: str,
    name: str = "",
    start_date: str = "2023-01-01",
    end_date: Optional[str] = None,
    initial_cash: float = INITIAL_CASH,
    *,
    log_every: int = 0,
) -> WalkForwardResult:
    """
    对单标的执行步进回测。

    :param log_every: 若 >0，每 N 根 15m 打印进度。
    """
    name = name or symbol
    end_ts = _parse_ts(end_date) if end_date else pd.Timestamp.now(tz=TZ_SH)
    start_ts = _parse_ts(start_date)
    if end_ts < start_ts:
        raise ValueError("end_date 不能早于 start_date")

    timeline = _load_timeline_15m(symbol, start_ts, end_ts)
    if not timeline:
        logging.warning("walk_forward: 时间轴为空，请检查本地 15m K 线与日期区间")

    cash = float(initial_cash)
    shares = 0.0
    entry_price: Optional[float] = None
    trades: List[Dict[str, Any]] = []
    equity_series: List[Tuple[str, float]] = []
    peak = -1.0
    max_dd = 0.0

    for i, t in enumerate(timeline):
        if log_every and i % log_every == 0:
            logging.info("walk_forward: [%d/%d] %s", i + 1, len(timeline), _bar_index_end_str(t))

        sym_d, sym_60, sym_15 = _fetch_triple_at_t(symbol, t)
        ix_d, ix_60, ix_15 = _fetch_index_triple_at_t(t)
        market_info = _compute_market_state(ix_d, ix_60, ix_15)
        market_state = market_info["state"]

        holding_codes: set[str] = set()
        if shares > 1e-9:
            holding_codes.add(symbol)

        close_px = _close_at_t(sym_15, t)
        if close_px is None:
            eq_flat = cash if shares < 1e-9 else (equity_series[-1][1] if equity_series else float(initial_cash))
            equity_series.append((_bar_index_end_str(t), round(eq_flat, 2)))
            continue

        action = "观望"
        try:
            pt = pd.Timestamp(t).tz_convert(TZ_SH).tz_localize(None)
            snapshot_ts = datetime(
                int(pt.year),
                int(pt.month),
                int(pt.day),
                int(pt.hour),
                int(pt.minute),
                int(pt.second),
                int(pt.microsecond),
            )
            analysis, buy_signals, _state = _run_symbol_analysis_pipeline(
                symbol, holding_codes, market_state, sym_d, sym_60, sym_15
            )
            snap = build_snapshot_data(
                timestamp=snapshot_ts,
                code=symbol,
                name=name,
                market_state=market_state,
                analysis=analysis,
                h60_result=sym_60,
                h15_result=sym_15,
                sell_signals=analysis["h60_sell_signals"],
                buy_signals=buy_signals,
            )
            action = snap.get("实际交易动作") or "观望"
        except Exception:
            logging.debug("walk_forward: 信号计算失败 @ %s", _bar_index_end_str(t), exc_info=True)

        eq_before = cash + shares * close_px
        if peak < 0:
            peak = eq_before
        peak = max(peak, eq_before)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq_before) / peak)

        if action == "买入" and shares < 1e-9 and cash > 1e-9:
            shares = cash / close_px
            entry_price = close_px
            trades.append(
                {
                    "时间": _bar_index_end_str(t),
                    "方向": "买入",
                    "价格": round(close_px, 4),
                    "金额": round(cash, 2),
                    "单笔盈亏比例": None,
                }
            )
            cash = 0.0
        elif action == "卖出" and shares > 1e-9:
            cash = shares * close_px
            pnl = None
            if entry_price and entry_price > 0:
                pnl = round((close_px - entry_price) / entry_price * 100, 4)
            trades.append(
                {
                    "时间": _bar_index_end_str(t),
                    "方向": "卖出",
                    "价格": round(close_px, 4),
                    "金额": round(cash, 2),
                    "单笔盈亏比例": pnl,
                }
            )
            shares = 0.0
            entry_price = None

        eq = cash + shares * close_px
        equity_series.append((_bar_index_end_str(t), round(eq, 2)))
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    if not equity_series:
        final_eq = float(initial_cash)
    else:
        last_close: Optional[float] = None
        if timeline:
            _d, _m, last_h15 = _fetch_triple_at_t(symbol, timeline[-1])
            last_close = _close_at_t(last_h15, timeline[-1])
        if last_close is not None:
            final_eq = cash + shares * last_close
        else:
            final_eq = float(equity_series[-1][1])

    return WalkForwardResult(
        symbol=symbol,
        name=name,
        start=start_date,
        end=end_ts.tz_convert(TZ_SH).strftime("%Y-%m-%d %H:%M:%S"),
        initial_cash=float(initial_cash),
        final_equity=round(final_eq, 2),
        max_drawdown_pct=round(max_dd * 100, 4),
        trades=trades,
        equity_series=equity_series,
    )


def _print_report(res: WalkForwardResult) -> None:
    print("\n========== Walk-Forward 回测战报 ==========\n")
    print(f"标的: {res.name} ({res.symbol})")
    print(f"区间: {res.start} ~ {res.end}")
    print(f"初始资金: {res.initial_cash:,.2f} 元")
    print(f"期末权益: {res.final_equity:,.2f} 元")
    print(f"最大回撤: {res.max_drawdown_pct:.4f}%（按标记市值、含浮动盈亏）\n")
    print("--- 成交明细 ---\n")
    if not res.trades:
        print("（无买卖成交）\n")
    else:
        for j, tr in enumerate(res.trades, 1):
            pnl = tr.get("单笔盈亏比例")
            pnl_s = f"{pnl:.4f}%" if pnl is not None else "-"
            print(
                f"{j}. {tr['时间']} | {tr['方向']} | 价 {tr['价格']} | "
                f"现金流 {tr['金额']:,.2f} 元 | 单笔盈亏 {pnl_s}"
            )
    print("\n==========================================\n")


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="15m 步进回测（复用实盘信号管线）")
    p.add_argument("--symbol", default="510300", help="标的代码，默认 510300")
    p.add_argument("--name", default="沪深300ETF", help="显示名称")
    p.add_argument("--start", default="2023-01-01", help="回测开始日期")
    p.add_argument("--end", default="", help="结束时刻，默认当前；格式 YYYY-MM-DD 或含时分秒")
    p.add_argument("--cash", type=float, default=INITIAL_CASH, help="初始资金（元）")
    p.add_argument("--log-every", type=int, default=0, help="每 N 根 K 打一条进度日志，0 表示不打")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    end = args.end.strip() or None
    res = run_walk_forward_backtest(
        args.symbol,
        name=args.name,
        start_date=args.start,
        end_date=end,
        initial_cash=args.cash,
        log_every=args.log_every,
    )
    _print_report(res)


if __name__ == "__main__":
    main()
