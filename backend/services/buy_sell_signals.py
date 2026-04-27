"""
60分钟缠论买卖信号批量计算模块（供定时调度调用）

与「破」字标记实现逻辑一致：
- 定时调度在每次 60m/日线同步后批量计算所有标的的买卖信号
- 结果写入 buy_sell_signals.json，前端刷新页面后直接读取

覆盖信号：
- 买：一买、二买、三买（任意一种出现即标记「买」）
- 卖：一卖、二卖、三卖（任意一种出现即标记「卖」）

一买复用 services.first_buy_point.detect_first_buy_point
其余信号基于 hourlyBuySellSignals.ts 核心逻辑翻译为 Python
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.defense_radar import radar_output_dir
from services.first_buy_point import (
    calculate_macd_green_area,
    check_macd_zero_axis_retrace,
    detect_first_buy_point,
    find_down_pens,
    find_downward_hubs,
    has_bottom_fractal,
)
from services.indicators import get_index_kline

BUY_SELL_SIGNALS_JSON = "buy_sell_signals.json"

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _load_watchlist_observation_symbols() -> List[Tuple[str, str]]:
    """读取 watchlist.json 和 observation.json，返回 (code, name) 列表。"""
    symbols: List[Tuple[str, str]] = []
    root = Path(__file__).resolve().parents[2]

    watchlist_path = root / "backend" / "data" / "watchlist.json"
    if watchlist_path.is_file():
        try:
            data = json.loads(watchlist_path.read_text(encoding="utf-8"))
            for item in data.get("holdings", []):
                if isinstance(item, dict) and item.get("code"):
                    symbols.append((str(item["code"]).strip(), str(item.get("name", "")).strip()))
        except Exception:  # noqa: BLE001
            logging.warning("buy_sell_signals: 读取 watchlist.json 失败")

    observation_path = root / "backend" / "data" / "observation.json"
    if observation_path.is_file():
        try:
            data = json.loads(observation_path.read_text(encoding="utf-8"))
            for item in data.get("observations", []):
                if isinstance(item, dict) and item.get("code"):
                    code = str(item["code"]).strip()
                    name = str(item.get("name", "")).strip()
                    if not any(c == code for c, _ in symbols):
                        symbols.append((code, name))
        except Exception:  # noqa: BLE001
            logging.warning("buy_sell_signals: 读取 observation.json 失败")

    return symbols


def _build_date_to_idx(data: List[Dict[str, Any]]) -> Dict[str, int]:
    return {item["date"]: i for i, item in enumerate(data)}


def _sort_centrals_for_hourly(centrals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(centrals, key=lambda c: (c["start_date"], c["end_date"]))


# ---------------------------------------------------------------------------
# 第一类买点（一买）—— 复用 first_buy_point 核心逻辑，适配 data/centrals/pens/fractals 接口
# ---------------------------------------------------------------------------

def _detect_first_buy_point(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检测一买信号（趋势底背驰）。
    返回: (has_signal, info)
    info 包含 date, stop_loss, area_ratio。
    """
    if not data or not centrals or not pens_effective or not fractals:
        return False, None

    # 1. 识别向下中枢（至少2个）
    downward_hubs = find_downward_hubs(centrals, pens_effective)
    if len(downward_hubs) < 2:
        return False, None

    hub_a = downward_hubs[-2]
    hub_b = downward_hubs[-1]

    # 2. 获取向下笔
    down_pens = find_down_pens(pens_effective)
    if len(down_pens) < 2:
        return False, None

    # 找到 B 中枢后的向下笔（c 段）：结束时间在 B 中枢之后，且创新低
    hub_b_end = hub_b["end_date"]
    hub_b_low = float(hub_b.get("zd", 0) or 0)
    c_pen = None
    for pen in down_pens:
        pen_end = pen.get("end_date")
        pen_low = min(float(pen.get("start_price", 0) or 0), float(pen.get("end_price", 0) or 0))
        if pen_end > hub_b_end and pen_low < hub_b_low:
            c_pen = pen
            break
    if not c_pen:
        return False, None

    # 找到 c 段之前的向下笔（b 段）
    hub_a_end = hub_a["end_date"]
    b_pen = None
    for pen in down_pens:
        if pen.get("end_date") > hub_a_end and pen.get("end_date") < c_pen.get("start_date"):
            b_pen = pen
    if not b_pen:
        return False, None

    # 3. 创新低检查
    c_low = min(float(c_pen.get("start_price", 0) or 0), float(c_pen.get("end_price", 0) or 0))
    if c_low >= hub_b_low:
        return False, None

    # 4. B 中枢构建期间 MACD 回抽零轴
    if not check_macd_zero_axis_retrace(data, hub_b["start_date"], hub_b["end_date"]):
        return False, None

    # 5. MACD 绿柱面积
    b_area = calculate_macd_green_area(data, b_pen.get("start_date"), b_pen.get("end_date"))
    c_area = calculate_macd_green_area(data, c_pen.get("start_date"), c_pen.get("end_date"))
    if b_area <= 0 or c_area <= 0:
        return False, None
    if c_area >= b_area:
        return False, None

    # 6. 底分型确认
    c_end_date = c_pen.get("end_date")
    if not has_bottom_fractal(data, c_end_date):
        return False, None

    # 止损线：底分型最低价
    stop_loss = c_low
    for item in data:
        if item.get("date") == c_end_date:
            stop_loss = item.get("low", c_low)
            break

    return True, {
        "date": c_end_date,
        "stop_loss": stop_loss,
        "area_ratio": c_area / b_area,
        "b_area": b_area,
        "c_area": c_area,
    }


# ---------------------------------------------------------------------------
# 第二类买点（二买）
# ---------------------------------------------------------------------------

def _detect_second_buy_point(
    data: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
    max_lookback_bars: int = 60,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检测二买信号。
    返回: (has_signal, info)
    info 包含 date 和 stop_loss，用于后续失效检查。
    """
    if not pens_effective or len(pens_effective) < 3 or len(data) < 10:
        return False, None

    date_to_idx = _build_date_to_idx(data)
    last_idx = len(data) - 1
    n = len(pens_effective)

    # 从后往前找已完成的向下笔作为"回踩笔"
    retracement_idx = -1
    for i in range(n - 1, -1, -1):
        pen = pens_effective[i]
        if pen["direction"] == "down":
            end_idx = date_to_idx.get(pen["end_date"])
            if end_idx is not None and end_idx < last_idx:
                retracement_idx = i
                break

    if retracement_idx < 2:
        return False, None

    # 回踩笔之前必须是向上笔
    rally_idx = retracement_idx - 1
    if pens_effective[rally_idx]["direction"] != "up":
        return False, None

    # 向上笔之前必须是向下笔（一买的 c 段）
    c_pen_idx = rally_idx - 1
    if pens_effective[c_pen_idx]["direction"] != "down":
        return False, None

    retracement_pen = pens_effective[retracement_idx]
    c_pen = pens_effective[c_pen_idx]

    # 一买在 max_lookback_bars 内
    c_end_idx = date_to_idx.get(c_pen["end_date"])
    if c_end_idx is None or last_idx - c_end_idx > max_lookback_bars:
        return False, None

    # 一买 c 段终点必须有底分型
    has_buy1_bottom = any(
        f["type"] == "bottom" and f["date"] == c_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_buy1_bottom:
        return False, None

    # 回踩不创新低
    retracement_low = min(retracement_pen["start_price"], retracement_pen["end_price"])
    c_low = min(c_pen["start_price"], c_pen["end_price"])
    if retracement_low < c_low:
        return False, None

    # 回踩终点有底分型
    has_bottom = any(
        f["type"] == "bottom" and f["date"] == retracement_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_bottom:
        return False, None

    # MACD 动能过滤
    def calc_green_area(pen: Dict[str, Any]) -> float:
        s_idx = date_to_idx.get(pen["start_date"])
        e_idx = date_to_idx.get(pen["end_date"])
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        area = 0.0
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None and m < 0:
                area += abs(m)
        return area

    c_area = calc_green_area(c_pen)
    retracement_area = calc_green_area(retracement_pen)
    macd_weaker = retracement_area < c_area

    # 或者 MACD 黄白线在 0 轴上方（强势二买）
    retracement_end_idx = date_to_idx.get(retracement_pen["end_date"])
    macd_above_zero = False
    if retracement_end_idx is not None:
        m = data[retracement_end_idx].get("macd")
        if m is not None and m.get("dif", 0) > 0 and m.get("dea", 0) > 0:
            macd_above_zero = True

    if not macd_weaker and not macd_above_zero:
        return False, None

    # 回撤深度过滤
    rally_high = max(pens_effective[rally_idx]["start_price"], pens_effective[rally_idx]["end_price"])
    if rally_high <= c_low:
        return False, None
    retracement_depth = (rally_high - retracement_low) / (rally_high - c_low)
    if retracement_depth > 0.8:
        return False, None

    # 止损线
    stop_loss = (
        data[retracement_end_idx]["low"]
        if retracement_end_idx is not None and 0 <= retracement_end_idx < len(data)
        else retracement_pen["end_price"]
    )

    return True, {"date": retracement_pen["end_date"], "stop_loss": stop_loss, "buy1_date": c_pen["end_date"], "buy1_stop": c_low}


# ---------------------------------------------------------------------------
# 第三类买点（三买）
# ---------------------------------------------------------------------------

def _detect_third_buy_point(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检测三买信号。
    返回: (has_signal, info)
    info 包含 date 和 stop_loss，用于后续失效检查。
    """
    if not centrals or len(centrals) == 0 or not pens_effective or len(pens_effective) < 2 or len(data) < 10:
        return False, None

    sorted_centrals = _sort_centrals_for_hourly(centrals)
    base_hub = sorted_centrals[-1]
    zg = float(base_hub.get("zg", 0))
    if not zg or not zg > 0:
        return False, None

    date_to_idx = _build_date_to_idx(data)
    hub_end_idx = date_to_idx.get(base_hub["end_date"])
    if hub_end_idx is None:
        return False, None

    pens_after_hub = [p for p in pens_effective if date_to_idx.get(p["start_date"], -1) > hub_end_idx]
    if len(pens_after_hub) < 2:
        return False, None

    # 确认暴力突破：存在向上笔突破 ZG
    breakout_pen = None
    for pen in pens_after_hub:
        if pen["direction"] == "up":
            high = max(pen["start_price"], pen["end_price"])
            if high > zg:
                breakout_pen = pen
                break
    if breakout_pen is None:
        return False, None

    # 锁定洗盘回踩：突破后存在向下笔
    breakout_end_idx = date_to_idx.get(breakout_pen["end_date"])
    if breakout_end_idx is None:
        return False, None

    pullback_pen = None
    for pen in pens_after_hub:
        s_idx = date_to_idx.get(pen["start_date"])
        if s_idx is not None and s_idx > breakout_end_idx and pen["direction"] == "down":
            pullback_pen = pen
            break
    if pullback_pen is None:
        return False, None

    # 核心空间判定：悬空回踩（最低点严格大于 ZG）
    pullback_low = min(pullback_pen["start_price"], pullback_pen["end_price"])
    eps = 1e-4
    if pullback_low <= zg + eps:
        return False, None

    # 底分型确认
    has_bottom = any(
        f["type"] == "bottom" and f["date"] == pullback_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_bottom:
        return False, None

    # 突破动能校验
    breakout_start_idx = date_to_idx.get(breakout_pen["start_date"])
    has_breakout_momentum = False
    if breakout_start_idx is not None and breakout_end_idx is not None:
        red_area = 0.0
        dif_crossed_zero = False
        prev_dif = None
        for i in range(breakout_start_idx, breakout_end_idx + 1):
            m = data[i].get("macd")
            if m is not None:
                if m.get("macd", 0) > 0:
                    red_area += m["macd"]
                if prev_dif is not None and prev_dif <= 0 and m.get("dif", 0) > 0:
                    dif_crossed_zero = True
                prev_dif = m.get("dif")
        has_breakout_momentum = red_area > 0.5 or dif_crossed_zero
    if not has_breakout_momentum:
        return False, None

    # MACD 动能过滤（水上漂）：回踩终点 DIF>0 且 DEA>0
    pullback_end_idx = date_to_idx.get(pullback_pen["end_date"])
    macd_water_above = False
    if pullback_end_idx is not None:
        m = data[pullback_end_idx].get("macd")
        if m is not None and m.get("dif", 0) > 0 and m.get("dea", 0) > 0:
            macd_water_above = True
    if not macd_water_above:
        return False, None

    # 止损线
    stop_loss = (
        data[pullback_end_idx]["low"]
        if pullback_end_idx is not None and 0 <= pullback_end_idx < len(data)
        else pullback_pen["end_price"]
    )

    return True, {"date": pullback_pen["end_date"], "stop_loss": stop_loss}


# ---------------------------------------------------------------------------
# 第一类卖点（一卖）
# ---------------------------------------------------------------------------

def _detect_first_sell_point(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    if not centrals or len(centrals) < 2 or not pens_effective or len(pens_effective) < 2 or len(data) < 10:
        return False, None

    upward_hubs = [
        c for c in _sort_centrals_for_hourly(centrals)
        if next((p for p in pens_effective if p["start_date"] == c["start_date"]), {}).get("direction") == "up"
    ]
    if len(upward_hubs) < 2:
        return False, None

    hub_a = upward_hubs[-2]
    hub_b = upward_hubs[-1]

    up_pens = sorted([p for p in pens_effective if p["direction"] == "up"], key=lambda p: p["start_date"])
    if len(up_pens) < 2:
        return False, None

    pens_after_hub_b = [p for p in up_pens if p["start_date"] > hub_b["end_date"]]
    if not pens_after_hub_b:
        return False, None
    c_pen = pens_after_hub_b[-1]

    b_pen = next(
        (p for p in up_pens if p["end_date"] > hub_a["end_date"] and p["end_date"] < c_pen["start_date"]),
        None
    )
    if b_pen is None:
        return False, None

    c_high = max(c_pen["start_price"], c_pen["end_price"])
    hub_b_high = float(hub_b.get("zg", 0))
    if c_high <= hub_b_high:
        return False, None

    date_to_idx = _build_date_to_idx(data)
    hub_b_start_idx = date_to_idx.get(hub_b["start_date"])
    hub_b_end_idx = date_to_idx.get(hub_b["end_date"])
    if hub_b_start_idx is None or hub_b_end_idx is None:
        return False, None

    macd_retraced_zero = False
    for i in range(hub_b_start_idx, hub_b_end_idx + 1):
        m = data[i].get("macd")
        if m is not None and (m.get("dif", 1) <= 0 or m.get("macd", 1) <= 0):
            macd_retraced_zero = True
            break
    if not macd_retraced_zero:
        return False, None

    def calc_red_area(pen: Dict[str, Any]) -> float:
        s_idx = date_to_idx.get(pen["start_date"])
        e_idx = date_to_idx.get(pen["end_date"])
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        area = 0.0
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None and m > 0:
                area += abs(m)
        return area

    b_area = calc_red_area(b_pen)
    c_area = calc_red_area(c_pen)
    if b_area <= 0 or c_area <= 0 or c_area >= b_area:
        return False, None

    has_top = any(
        f["type"] == "top" and f["date"] == c_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_top:
        return False, None

    c_end_idx = date_to_idx.get(c_pen["end_date"])
    if c_end_idx is None:
        return False, None
    bars_since_end = len(data) - 1 - c_end_idx
    if bars_since_end > 20:
        return False, None

    stop_loss = data[c_end_idx]["high"] if 0 <= c_end_idx < len(data) else c_pen["end_price"]
    return True, {"date": c_pen["end_date"], "high": c_high, "stop_loss": stop_loss}


# ---------------------------------------------------------------------------
# 第二类卖点（二卖）
# ---------------------------------------------------------------------------

def _detect_second_sell_point(
    data: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
    max_lookback_bars: int = 60,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    if not pens_effective or len(pens_effective) < 3 or len(data) < 10:
        return False, None

    date_to_idx = _build_date_to_idx(data)
    last_idx = len(data) - 1
    n = len(pens_effective)

    rebound_idx = -1
    for i in range(n - 1, -1, -1):
        pen = pens_effective[i]
        if pen["direction"] == "up":
            end_idx = date_to_idx.get(pen["end_date"])
            if end_idx is not None and end_idx < last_idx:
                rebound_idx = i
                break

    if rebound_idx < 2:
        return False, None

    drop_idx = rebound_idx - 1
    if pens_effective[drop_idx]["direction"] != "down":
        return False, None

    c_pen_idx = drop_idx - 1
    if pens_effective[c_pen_idx]["direction"] != "up":
        return False, None

    rebound_pen = pens_effective[rebound_idx]
    c_pen = pens_effective[c_pen_idx]

    c_end_idx = date_to_idx.get(c_pen["end_date"])
    if c_end_idx is None or last_idx - c_end_idx > max_lookback_bars:
        return False, None

    has_sell1_top = any(
        f["type"] == "top" and f["date"] == c_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_sell1_top:
        return False, None

    rebound_high = max(rebound_pen["start_price"], rebound_pen["end_price"])
    c_high = max(c_pen["start_price"], c_pen["end_price"])
    if rebound_high > c_high:
        return False, None

    has_top = any(
        f["type"] == "top" and f["date"] == rebound_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_top:
        return False, None

    def calc_red_area(pen: Dict[str, Any]) -> float:
        s_idx = date_to_idx.get(pen["start_date"])
        e_idx = date_to_idx.get(pen["end_date"])
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        area = 0.0
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None and m > 0:
                area += abs(m)
        return area

    c_area = calc_red_area(c_pen)
    rebound_area = calc_red_area(rebound_pen)
    macd_weaker = rebound_area < c_area

    rebound_end_idx = date_to_idx.get(rebound_pen["end_date"])
    macd_below_zero = False
    if rebound_end_idx is not None:
        m = data[rebound_end_idx].get("macd")
        if m is not None and m.get("dif", 0) < 0 and m.get("dea", 0) < 0:
            macd_below_zero = True

    if not macd_weaker and not macd_below_zero:
        return False, None

    stop_loss = (
        data[rebound_end_idx]["high"]
        if rebound_end_idx is not None and 0 <= rebound_end_idx < len(data)
        else rebound_pen["end_price"]
    )
    return True, {"date": rebound_pen["end_date"], "high": c_high, "stop_loss": stop_loss, "sell1_date": c_pen["end_date"]}


# ---------------------------------------------------------------------------
# 第三类卖点（三卖）
# ---------------------------------------------------------------------------

def _detect_third_sell_point(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
) -> bool:
    if not centrals or len(centrals) == 0 or not pens_effective or len(pens_effective) < 2 or len(data) < 10:
        return False

    sorted_centrals = _sort_centrals_for_hourly(centrals)
    base_hub = sorted_centrals[-1]
    zd = float(base_hub.get("zd", 0))
    if not zd or not zd > 0:
        return False

    date_to_idx = _build_date_to_idx(data)
    hub_end_idx = date_to_idx.get(base_hub["end_date"])
    if hub_end_idx is None:
        return False

    pens_after_hub = [p for p in pens_effective if date_to_idx.get(p["start_date"], -1) > hub_end_idx]
    if len(pens_after_hub) < 2:
        return False

    breakdown_pen = None
    for pen in pens_after_hub:
        if pen["direction"] == "down":
            low = min(pen["start_price"], pen["end_price"])
            if low < zd:
                breakdown_pen = pen
                break
    if breakdown_pen is None:
        return False

    breakdown_end_idx = date_to_idx.get(breakdown_pen["end_date"])
    if breakdown_end_idx is None:
        return False

    rebound_pen = None
    for pen in pens_after_hub:
        s_idx = date_to_idx.get(pen["start_date"])
        if s_idx is not None and s_idx > breakdown_end_idx and pen["direction"] == "up":
            rebound_pen = pen
            break
    if rebound_pen is None:
        return False

    rebound_high = max(rebound_pen["start_price"], rebound_pen["end_price"])
    if rebound_high >= zd:
        return False

    has_top = any(
        f["type"] == "top" and f["date"] == rebound_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_top:
        return False

    rebound_end_idx = date_to_idx.get(rebound_pen["end_date"])
    macd_water_below = False
    if rebound_end_idx is not None:
        m = data[rebound_end_idx].get("macd")
        if m is not None and m.get("dif", 0) < 0 and m.get("dea", 0) < 0:
            macd_water_below = True

    if not macd_water_below:
        return False

    # 时间邻近性检查（与一卖/二卖保持一致，只显示最近20根K线内的信号）
    if rebound_end_idx is not None:
        bars_since_end = len(data) - 1 - rebound_end_idx
        if bars_since_end > 20:
            return False

    return True


# ---------------------------------------------------------------------------
# 单个标的综合判断
# ---------------------------------------------------------------------------

def _detect_buy_sell_for_symbol(code: str, name: str = "") -> Tuple[bool, bool, Dict[str, Any]]:
    """
    检测单个标的的买卖信号
    返回: (has_buy, has_sell, details)
    """
    has_buy = False
    has_sell = False
    details: Dict[str, Any] = {
        "code": code,
        "name": name,
        "first_buy": False,
        "second_buy": False,
        "third_buy": False,
        "first_sell": False,
        "second_sell": False,
        "third_sell": False,
    }

    try:
        start_date_79d = (datetime.now() - timedelta(days=79)).strftime("%Y-%m-%d")
        result = get_index_kline(
            symbol=code,
            start_date=start_date_79d,
            period="60",
            refresh=False,
        )
    except Exception as e:
        logging.debug("buy_sell_signals: 获取 %s K线失败: %s", code, e)
        return has_buy, has_sell, details

    data = result.get("data", [])
    centrals = result.get("centrals", [])
    pens = result.get("pens", [])
    pens_effective = result.get("pens_effective", [])
    fractals = result.get("fractals", [])

    if not data or len(data) < 10:
        return has_buy, has_sell, details

    # 保存信号信息用于后续失效检查
    first_buy_info: Optional[Dict[str, Any]] = None
    second_buy_info: Optional[Dict[str, Any]] = None
    third_buy_info: Optional[Dict[str, Any]] = None

    # 一买（复用已有模块）
    raw_first_buy_info = None
    try:
        first_buy = detect_first_buy_point(code, name, refresh=False)
        if first_buy is not None:
            raw_first_buy_info = {
                "date": first_buy.date,
                "stop_loss": first_buy.stop_loss,
            }
            first_buy_info = raw_first_buy_info
            details["first_buy"] = True
            has_buy = True
    except Exception as e:
        logging.debug("buy_sell_signals: 一买检测 %s 失败: %s", code, e)

    # 二买
    try:
        second_buy_has, second_buy_info = _detect_second_buy_point(data, pens_effective, fractals)
        if second_buy_has:
            details["second_buy"] = True
            has_buy = True
    except Exception as e:
        logging.debug("buy_sell_signals: 二买检测 %s 失败: %s", code, e)

    # 三买
    try:
        third_buy_has, third_buy_info = _detect_third_buy_point(data, centrals, pens_effective, fractals)
        if third_buy_has:
            details["third_buy"] = True
            has_buy = True
    except Exception as e:
        logging.debug("buy_sell_signals: 三买检测 %s 失败: %s", code, e)

    # 保存卖信号信息用于后续失效检查
    first_sell_info: Optional[Dict[str, Any]] = None
    second_sell_info: Optional[Dict[str, Any]] = None

    # 一卖
    try:
        first_sell_has, first_sell_info = _detect_first_sell_point(data, centrals, pens_effective, fractals)
        if first_sell_has:
            details["first_sell"] = True
            has_sell = True
    except Exception as e:
        logging.debug("buy_sell_signals: 一卖检测 %s 失败: %s", code, e)

    # 二卖
    try:
        second_sell_has, second_sell_info = _detect_second_sell_point(data, pens_effective, fractals)
        if second_sell_has:
            details["second_sell"] = True
            has_sell = True
    except Exception as e:
        logging.debug("buy_sell_signals: 二卖检测 %s 失败: %s", code, e)

    # 三卖
    try:
        if _detect_third_sell_point(data, centrals, pens_effective, fractals):
            details["third_sell"] = True
            has_sell = True
    except Exception as e:
        logging.debug("buy_sell_signals: 三卖检测 %s 失败: %s", code, e)

    # ========== 与前端 computeHourlyBuySellState 过滤条件对齐 ==========
    # 获取日线数据计算 keepDailySupport
    keep_daily_support = False
    try:
        daily_start = (datetime.now() - timedelta(days=380)).strftime("%Y-%m-%d")
        daily_result = get_index_kline(
            symbol=code,
            start_date=daily_start,
            period="daily",
            refresh=False,
        )
        daily_centrals = daily_result.get("centrals", [])
        if daily_centrals and data:
            daily_azd = float(daily_centrals[0]["zd"])
            daily_czd = float(daily_centrals[-1]["zd"])
            absolute_bottom = min(daily_czd, daily_azd)
            keep_daily_support = data[-1]["close"] >= absolute_bottom
    except Exception:
        pass

    # 计算 macdBuy（与前端逻辑一致）
    macd_buy = False
    if len(data) >= 3:
        m0 = data[-1].get("macd", {}).get("macd")
        m1 = data[-2].get("macd", {}).get("macd")
        m2 = data[-3].get("macd", {}).get("macd")
        dif0 = data[-1].get("macd", {}).get("dif")
        dif1 = data[-2].get("macd", {}).get("dif")
        dea0 = data[-1].get("macd", {}).get("dea")
        dea1 = data[-2].get("macd", {}).get("dea")

        if (
            m0 is not None and m1 is not None and m2 is not None
            and dif0 is not None and dif1 is not None and dea0 is not None and dea1 is not None
        ):
            macd_green_short = m0 < 0 and abs(m0) < abs(m1)
            macd_green_to_red = m0 >= 0 and m1 < 0
            macd_red_len = m0 > 0 and m1 > 0 and m0 > m1

            macd_buy = (
                (macd_green_short or macd_green_to_red or macd_red_len)
                and (dif0 > dif1 or (dif1 <= dea1 and dif0 > dea0))
                and not (m0 < 0 and m1 < 0 and m2 < 0 and abs(m0) > abs(m1) and abs(m1) > abs(m2))
            )

    # 计算 inCCentral
    in_c_central = False
    if centrals and data:
        c = centrals[-1]
        c_zd = float(c["zd"])
        c_zg = float(c["zg"])
        last_close = data[-1]["close"]
        in_c_central = last_close >= c_zd and last_close <= c_zg

    # 计算 hasBottomDivInSwitch（当前向上笔内有底背驰点）
    has_bottom_div_in_switch = False
    pens_eff = result.get("pens_effective", [])
    if pens_eff and len(pens_eff) >= 2 and data:
        switched_down_to_up = (
            pens_eff[-2]["direction"] == "down" and pens_eff[-1]["direction"] == "up"
        )
        if switched_down_to_up:
            last_up_pen = pens_eff[-1]
            # 简化计算底背驰：比较最近两个向下笔的 MACD 绿柱面积
            down_pens = [p for p in pens_eff if p["direction"] == "down"]
            if len(down_pens) >= 2:
                last_down = down_pens[-1]
                prev_down = down_pens[-2]
                date_to_idx = _build_date_to_idx(data)
                s_idx = date_to_idx.get(last_down["start_date"])
                e_idx = date_to_idx.get(last_down["end_date"])
                ps_idx = date_to_idx.get(prev_down["start_date"])
                pe_idx = date_to_idx.get(prev_down["end_date"])
                if s_idx is not None and e_idx is not None and ps_idx is not None and pe_idx is not None:
                    last_area = sum(
                        abs(item.get("macd", {}).get("macd", 0))
                        for item in data[s_idx:e_idx + 1]
                        if item.get("macd", {}).get("macd", 0) < 0
                    )
                    prev_area = sum(
                        abs(item.get("macd", {}).get("macd", 0))
                        for item in data[ps_idx:pe_idx + 1]
                        if item.get("macd", {}).get("macd", 0) < 0
                    )
                    # 底背驰：最后一根向下笔的绿柱面积 < 前一根向下笔的绿柱面积
                    if last_area < prev_area:
                        # 检查背驰点是否在当前向上笔内
                        div_date = last_down["end_date"]
                        has_bottom_div_in_switch = (
                            div_date >= last_up_pen["start_date"] and div_date <= last_up_pen["end_date"]
                        )

    # 应用过滤条件（与前端 computeHourlyBuySellState 一致）
    # 一买：keepDailySupport && hasBottomDivInSwitch
    if details["first_buy"] and (not keep_daily_support or not has_bottom_div_in_switch):
        details["first_buy"] = False

    # 二买：keepDailySupport && macdBuy
    if details["second_buy"] and (not keep_daily_support or not macd_buy):
        details["second_buy"] = False

    # 三买：keepDailySupport && !inCCentral
    if details["third_buy"] and (not keep_daily_support or in_c_central):
        details["third_buy"] = False

    # ========== 买点失效检查（与前端一致） ==========
    def _check_buy_destroyed(buy_info: Optional[Dict[str, Any]]) -> bool:
        """检查买点是否已失效（后续收盘价跌破止损线）。"""
        if not buy_info or not buy_info.get("date") or not buy_info.get("stop_loss"):
            return False
        buy_date = buy_info["date"]
        stop_loss = buy_info["stop_loss"]
        buy_idx = -1
        for i, d in enumerate(data):
            if d.get("date") == buy_date:
                buy_idx = i
                break
        if buy_idx < 0:
            return False
        for i in range(buy_idx + 1, len(data)):
            if data[i].get("close", 0) < stop_loss:
                return True
        return False

    if details["first_buy"] and _check_buy_destroyed(first_buy_info):
        details["first_buy"] = False

    if details["second_buy"] and _check_buy_destroyed(second_buy_info):
        details["second_buy"] = False

    if details["third_buy"] and _check_buy_destroyed(third_buy_info):
        details["third_buy"] = False

    # ========== 卖点失效检查（与前端 computeHourlyBuySellState 一致） ==========
    # 规则1：一卖触发后，若后续K线高点突破一卖最高点，则一卖结构被破坏
    if details["first_sell"] and first_sell_info:
        sell1_high = first_sell_info.get("high", 0)
        sell1_date = first_sell_info.get("date", "")
        sell1_idx = -1
        for i, d in enumerate(data):
            if d.get("date") == sell1_date:
                sell1_idx = i
                break
        if sell1_idx >= 0:
            for i in range(sell1_idx + 1, len(data)):
                if data[i].get("high", 0) > sell1_high:
                    details["first_sell"] = False
                    break

    # 规则2：二卖依赖一卖存在，一卖失效则二卖必须同步失效
    if details["second_sell"] and not details["first_sell"]:
        details["second_sell"] = False

    # 规则3：二卖触发后，若后续K线高点突破一卖最高点，说明多头已破坏M头结构，二卖失效
    if details["second_sell"] and details["first_sell"] and second_sell_info:
        sell1_high = first_sell_info.get("high", 0) if first_sell_info else 0
        sell2_date = second_sell_info.get("date", "")
        sell2_idx = -1
        for i, d in enumerate(data):
            if d.get("date") == sell2_date:
                sell2_idx = i
                break
        if sell2_idx >= 0:
            for i in range(sell2_idx + 1, len(data)):
                if data[i].get("high", 0) > sell1_high:
                    details["second_sell"] = False
                    break

    # ===== 严格单向状态机互斥（核心修复：禁止时空穿越） =====
    # 状态机定义：0(初始) -> 1(一买确认) -> 2(二买确认) -> 3(三买确认/尝试中)
    # 流转方向严格单向，绝对禁止逆向流转（3 变回 2）
    # 三买失败后进入 CENTER_OSCILLATION，屏蔽一切买点信号
    # 重置条件：从三买触发日开始，价格向下跌破上一买的绝对最低点

    state_machine_locked = False
    center_oscillation = False

    # 检查三买是否已失效（用于判定 CENTER_OSCILLATION）
    third_buy_destroyed = _check_buy_destroyed(third_buy_info) if third_buy_info else False

    # 确定是否进入过 STATE_3（三买已确认/尝试中/失败），与前端语义对齐
    has_entered_state3 = bool(third_buy_info) or third_buy_destroyed

    # 获取上一买的绝对最低点（优先从 raw_first_buy_info，其次从 second_buy_info 携带的一买信息）
    buy1_low = raw_first_buy_info.get("stop_loss", 0) if raw_first_buy_info else 0
    buy1_date = raw_first_buy_info.get("date", "") if raw_first_buy_info else ""
    if buy1_low == 0 and second_buy_info and details.get("second_buy"):
        buy1_low = second_buy_info.get("buy1_stop", 0)
        buy1_date = second_buy_info.get("buy1_date", "")

    if has_entered_state3 and buy1_low > 0 and buy1_date:
        mutex_date = third_buy_info.get("date", "")

        if mutex_date and mutex_date > buy1_date:
            third_idx = -1
            for i, d in enumerate(data):
                if d.get("date") == mutex_date:
                    third_idx = i
                    break

            if third_idx >= 0:
                broke_new_low = False
                for i in range(third_idx + 1, len(data)):
                    low_val = data[i].get("low")
                    if low_val is not None and low_val < buy1_low:
                        broke_new_low = True
                        break

                if not broke_new_low:
                    state_machine_locked = True
                    # 三买失效后进入中枢震荡，屏蔽一切买点
                    if third_buy_destroyed:
                        center_oscillation = True

    # 应用互斥锁
    if state_machine_locked:
        # STATE_3 后绝对禁止二买（无论三买成功还是失败）
        if details["second_buy"]:
            details["second_buy"] = False

        # 三买失败后进入 CENTER_OSCILLATION，屏蔽一切买点
        if center_oscillation:
            if details["first_buy"]:
                details["first_buy"] = False
            if details["third_buy"]:
                details["third_buy"] = False

    # 重新计算 has_buy / has_sell
    has_buy = details["first_buy"] or details["second_buy"] or details["third_buy"]
    has_sell = details["first_sell"] or details["second_sell"] or details["third_sell"]

    return has_buy, has_sell, details


# ---------------------------------------------------------------------------
# 批量计算与持久化
# ---------------------------------------------------------------------------

def compute_and_save_buy_sell_signals() -> Path:
    """
    计算 watchlist + observation 中所有标的的买卖信号，保存到 buy_sell_signals.json
    由 kline_scheduler 在每次定时调度完成后调用
    """
    symbols = _load_watchlist_observation_symbols()
    if not symbols:
        logging.info("buy_sell_signals: watchlist 和 observation 均为空，跳过计算")
        out_dir = radar_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / BUY_SELL_SIGNALS_JSON
        path.write_text(
            json.dumps(
                {"generated_at": datetime.now().replace(microsecond=0).isoformat(), "buy_codes": [], "sell_codes": [], "details": []},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return path

    buy_codes: List[str] = []
    sell_codes: List[str] = []
    details: List[Dict[str, Any]] = []

    for code, name in symbols:
        has_buy, has_sell, detail = _detect_buy_sell_for_symbol(code, name)
        if has_buy:
            buy_codes.append(code)
        if has_sell:
            sell_codes.append(code)
        details.append(detail)

    payload = {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "buy_codes": buy_codes,
        "sell_codes": sell_codes,
        "details": details,
    }

    out_dir = radar_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / BUY_SELL_SIGNALS_JSON
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    logging.info(
        "buy_sell_signals: 买卖信号已写入 %s（%d 个标的，%d 个买，%d 个卖）",
        path, len(symbols), len(buy_codes), len(sell_codes),
    )
    return path


def load_buy_sell_signals_json(radar_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """读取 buy_sell_signals.json，供 API 接口使用"""
    d = radar_dir or radar_output_dir()
    path = d / BUY_SELL_SIGNALS_JSON
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logging.warning("buy_sell_signals: 读取 %s 失败", path)
        return None