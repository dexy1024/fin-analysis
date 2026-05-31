"""
60分钟级别缠论买卖点纯检测（无 batch/IO 依赖）。

供 buy_sell_signals、trade_command_engine 共用，避免循环导入。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.first_buy_point import (
    calculate_macd_green_area,
    check_macd_zero_axis_retrace,
    find_down_pens,
    find_downward_hubs,
    has_bottom_fractal,
)


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

    # 7. 时间邻近性检查（与前端保持一致，只显示最近20根K线内的信号）
    date_to_idx = _build_date_to_idx(data)
    c_end_idx = date_to_idx.get(c_end_date)
    if c_end_idx is not None:
        bars_since_end = len(data) - 1 - c_end_idx
        if bars_since_end > 20:
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

    # 买点检测：不做MACD和回撤深度过滤，检测到什么就显示什么
    # 客观缠论信号应保持原始检测结果

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

    pullback_end_idx = date_to_idx.get(pullback_pen["end_date"])

    # 时间邻近性检查（与一买/一卖保持一致，只显示最近20根K线内的信号）
    if pullback_end_idx is not None:
        bars_since_end = len(data) - 1 - pullback_end_idx
        if bars_since_end > 20:
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

    # c段高点取笔终点的收盘价（与前端显示一致）
    c_high = c_pen["end_price"]
    
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

    # 一卖c段终点必须有顶分型（与前端保持一致）
    has_sell1_top = any(
        f["type"] == "top" and f["date"] == c_pen["end_date"]
        for f in (fractals or [])
    )
    if not has_sell1_top:
        logging.debug("_detect_second_sell_point: 一卖顶分型检查失败，c_pen_end=%s, fractals=%s",
                      c_pen["end_date"], [f.get("date") for f in (fractals or []) if f.get("type") == "top"])
        return False, None

    rebound_high = max(rebound_pen["start_price"], rebound_pen["end_price"])
    c_high = max(c_pen["start_price"], c_pen["end_price"])
    if rebound_high > c_high:
        return False, None

    # 反弹终点有顶分型
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

    # 卖点检测：不做MACD过滤，检测到什么就显示什么
    # 客观缠论信号应保持原始检测结果

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

    # 卖点检测：不做MACD过滤，检测到什么就显示什么
    # 客观缠论信号应保持原始检测结果

    rebound_end_idx = date_to_idx.get(rebound_pen["end_date"])

    # 时间邻近性检查（与一卖/二卖保持一致，只显示最近20根K线内的信号）
    if rebound_end_idx is not None:
        bars_since_end = len(data) - 1 - rebound_end_idx
        if bars_since_end > 20:
            return False

    return True
