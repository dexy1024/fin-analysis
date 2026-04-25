"""
无头（Headless）多级别缠论量化黑盒报告引擎

核心架构：
- 双轨并行：独立后台脚本，绝对禁止触碰前端 UI
- 极速算力：各级别数据 limit 锁定约 250 根 K 线，算完即释放内存
- 资金铁律：所有交易指令硬编码基于 50,000 元人民币满仓额度

三层风控体系：
1. 全局大盘风控（上证指数 000001.SH）：MARKET_DEAD / MARKET_DANGER / MARKET_SAFE
2. 个股三维区间套：日线防线 → 60m 战役阵地 → 15m 微观狙击
3. 终极状态机：SELL / BUY / HOLD / IGNORE

集成：由 kline_scheduler 在每次定时槽位执行后调用
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

FIXED_TRADE_AMOUNT = 50_000  # 硬编码操作金额（元）
_EPS = 1e-9  # 浮点比较容差（与 indicators.py 一致）

# 项目根目录（backend/services/ 的上两级）
ROOT_DIR = Path(__file__).resolve().parents[2]
TRADE_REPORT_DIR = ROOT_DIR / "trade_reports"

# 上证指数代码
INDEX_CODE = "sh000001"
INDEX_NAME = "上证指数"


# ---------------------------------------------------------------------------
# 监控池加载（复用 buy_sell_signals 逻辑）
# ---------------------------------------------------------------------------

def _load_watchlist_observation_symbols() -> List[Tuple[str, str]]:
    """读取 watchlist.json 和 observation.json，返回去重后的 (code, name) 列表。"""
    symbols: List[Tuple[str, str]] = []

    watchlist_path = ROOT_DIR / "backend" / "data" / "watchlist.json"
    if watchlist_path.is_file():
        try:
            data = json.loads(watchlist_path.read_text(encoding="utf-8"))
            for item in data.get("holdings", []):
                if isinstance(item, dict) and item.get("code"):
                    symbols.append((str(item["code"]).strip(), str(item.get("name", "")).strip()))
        except Exception:  # noqa: BLE001
            logging.warning("trade_command_engine: 读取 watchlist.json 失败")

    observation_path = ROOT_DIR / "backend" / "data" / "observation.json"
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
            logging.warning("trade_command_engine: 读取 observation.json 失败")

    return symbols


# ---------------------------------------------------------------------------
# 持仓加载（复用 position_manager 逻辑）
# ---------------------------------------------------------------------------

def _load_holding_codes() -> set[str]:
    """读取 positions.json，返回当前持仓中的 code 集合。"""
    positions_file = ROOT_DIR / "data" / "positions.json"
    if not positions_file.is_file():
        return set()
    try:
        with open(positions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(p["code"]).strip() for p in data if isinstance(p, dict) and p.get("status") == "holding"}
    except Exception:  # noqa: BLE001
        logging.warning("trade_command_engine: 读取 positions.json 失败")
        return set()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _build_date_to_idx(data: List[Dict[str, Any]]) -> Dict[str, int]:
    """根据 data 中每条记录的 date 字段构建索引字典。"""
    return {item["date"]: i for i, item in enumerate(data)}


# 250 根 K 线数据范围（确保缠论结构有效的前提下尽量接近 250 根）
def _daily_start_date() -> str:
    """日线约 250 个交易日 ≈ 350 个自然日。"""
    return (datetime.now() - timedelta(days=350)).strftime("%Y-%m-%d")


def _h60_start_date() -> str:
    """60分钟：每天 8 根，250 根 ≈ 31 个交易日 ≈ 50 个自然日。"""
    return (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")


def _h15_start_date() -> str:
    """15分钟：每天 16 根，250 根 ≈ 16 个交易日 ≈ 25 个自然日。"""
    return (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 15分钟背驰检测
# ---------------------------------------------------------------------------

def _detect_15m_bottom_divergence(data: List[Dict[str, Any]], pens_effective: List[Dict[str, Any]]) -> bool:
    """
    15分钟底背驰：最近两个向下笔 MACD 绿柱面积缩小，且背驰点在当前向上笔内。
    """
    if not pens_effective or len(pens_effective) < 2 or not data:
        return False

    if pens_effective[-1]["direction"] != "up":
        return False

    last_up_pen = pens_effective[-1]
    down_pens = [p for p in pens_effective if p["direction"] == "down"]
    if len(down_pens) < 2:
        return False

    last_down = down_pens[-1]
    prev_down = down_pens[-2]

    date_to_idx = _build_date_to_idx(data)
    s_idx = date_to_idx.get(last_down["start_date"])
    e_idx = date_to_idx.get(last_down["end_date"])
    ps_idx = date_to_idx.get(prev_down["start_date"])
    pe_idx = date_to_idx.get(prev_down["end_date"])

    if s_idx is None or e_idx is None or ps_idx is None or pe_idx is None:
        return False

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

    if last_area < prev_area:
        div_date = last_down["end_date"]
        return div_date >= last_up_pen["start_date"] and div_date <= last_up_pen["end_date"]

    return False


def _detect_15m_top_divergence(data: List[Dict[str, Any]], pens_effective: List[Dict[str, Any]]) -> bool:
    """
    15分钟顶背驰：最近两个向上笔 MACD 红柱面积缩小，且背驰点在当前向下笔内。
    """
    if not pens_effective or len(pens_effective) < 2 or not data:
        return False

    if pens_effective[-1]["direction"] != "down":
        return False

    last_down_pen = pens_effective[-1]
    up_pens = [p for p in pens_effective if p["direction"] == "up"]
    if len(up_pens) < 2:
        return False

    last_up = up_pens[-1]
    prev_up = up_pens[-2]

    date_to_idx = _build_date_to_idx(data)
    s_idx = date_to_idx.get(last_up["start_date"])
    e_idx = date_to_idx.get(last_up["end_date"])
    ps_idx = date_to_idx.get(prev_up["start_date"])
    pe_idx = date_to_idx.get(prev_up["end_date"])

    if s_idx is None or e_idx is None or ps_idx is None or pe_idx is None:
        return False

    last_area = sum(
        item.get("macd", {}).get("macd", 0)
        for item in data[s_idx:e_idx + 1]
        if item.get("macd", {}).get("macd", 0) > 0
    )
    prev_area = sum(
        item.get("macd", {}).get("macd", 0)
        for item in data[ps_idx:pe_idx + 1]
        if item.get("macd", {}).get("macd", 0) > 0
    )

    if last_area < prev_area:
        div_date = last_up["end_date"]
        return div_date >= last_down_pen["start_date"] and div_date <= last_down_pen["end_date"]

    return False


# ---------------------------------------------------------------------------
# 60分钟买点/卖点条件计算
# ---------------------------------------------------------------------------

def _compute_h60_conditions(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    计算60分钟核心条件，返回字典：
    - in_c_central: 现价在 C 中枢内（ZD～ZG）
    - switched_down_to_up: 有效笔：前一下笔、当前上笔
    - has_bottom_div_in_switch: 底背驰点落在当前向上笔内
    - macd_buy: MACD 转强
    - last_pen_up: 最后一笔有效笔方向为向上
    - switched_up_to_down: 向上笔转向下笔（卖点信号）
    """
    result = {
        "in_c_central": False,
        "switched_down_to_up": False,
        "has_bottom_div_in_switch": False,
        "macd_buy": False,
        "last_pen_up": False,
        "switched_up_to_down": False,
    }

    if not data or len(data) < 3:
        return result

    last_close = data[-1]["close"]

    # in_c_central：中枢列表按距离现价排序，必须按形成时间重新排序取最新（真正的C中枢）
    if centrals:
        sorted_c = sorted(
            centrals,
            key=lambda c: c.get("form_end_date", c.get("end_date", "")),
        )
        c = sorted_c[-1]
        c_zd = float(c.get("zd", 0))
        c_zg = float(c.get("zg", 0))
        if c_zd and c_zg:
            result["in_c_central"] = c_zd <= last_close <= c_zg

    # 笔方向与转向
    if pens_effective and len(pens_effective) >= 2:
        result["switched_down_to_up"] = (
            pens_effective[-2]["direction"] == "down" and pens_effective[-1]["direction"] == "up"
        )
        result["switched_up_to_down"] = (
            pens_effective[-2]["direction"] == "up" and pens_effective[-1]["direction"] == "down"
        )
        result["last_pen_up"] = pens_effective[-1]["direction"] == "up"

    # has_bottom_div_in_switch
    if pens_effective and len(pens_effective) >= 2 and data:
        if pens_effective[-2]["direction"] == "down" and pens_effective[-1]["direction"] == "up":
            last_up_pen = pens_effective[-1]
            down_pens = [p for p in pens_effective if p["direction"] == "down"]
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
                    if last_area < prev_area:
                        div_date = last_down["end_date"]
                        result["has_bottom_div_in_switch"] = (
                            div_date >= last_up_pen["start_date"] and div_date <= last_up_pen["end_date"]
                        )

    # macd_buy
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

            result["macd_buy"] = (
                (macd_green_short or macd_green_to_red or macd_red_len)
                and (dif0 > dif1 or (dif1 <= dea1 and dif0 > dea0))
                and not (m0 < 0 and m1 < 0 and m2 < 0 and abs(m0) > abs(m1) and abs(m1) > abs(m2))
            )

    return result


# ---------------------------------------------------------------------------
# 第一层：全局大盘风控总闸
# ---------------------------------------------------------------------------

def _compute_market_state(
    index_daily: Optional[Dict[str, Any]],
    index_h60: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    计算上证指数全局风控状态，返回三级风控之一：
    - MARKET_DEAD:   现价 < A-ZD（极度危险，全局熔断）
    - MARKET_DANGER: A-ZD <= 现价 < C-ZD，或 60m 触发卖点（警戒，禁止开新仓）
    - MARKET_SAFE:   现价 >= C-ZD 且 60m 无卖点（安全，个股独立运行）
    """
    # 故障安全原则：数据缺失时默认最保守策略（警戒，禁止开新仓）
    result = {
        "state": "MARKET_DANGER",
        "price": None,
        "c_zd": None,
        "a_zd": None,
        "reason": "大盘数据不足，默认进入警戒状态，禁止开新仓",
    }

    # 日线分析
    if not index_daily or not index_daily.get("centrals") or not index_daily.get("data"):
        return result

    daily_centrals = index_daily["centrals"]
    daily_data = index_daily["data"]
    if not daily_centrals or not daily_data:
        return result

    # 中枢列表按距离现价排序，必须按形成时间重新排序：最早为 A，最晚为 C
    sorted_centrals = sorted(
        daily_centrals,
        key=lambda c: c.get("form_end_date", c.get("end_date", "")),
    )
    daily_azd = float(sorted_centrals[0]["zd"])
    daily_czd = float(sorted_centrals[-1]["zd"])
    daily_close = float(daily_data[-1]["close"])

    result["price"] = daily_close
    result["c_zd"] = daily_czd
    result["a_zd"] = daily_azd

    # 60分钟卖点检测：复用 buy_sell_signals 的一卖/二卖/三卖检测
    h60_sell_triggered = False
    if index_h60 and index_h60.get("data") and index_h60.get("centrals") and index_h60.get("pens_effective"):
        try:
            from services.buy_sell_signals import (
                _detect_first_sell_point,
                _detect_second_sell_point,
                _detect_third_sell_point,
            )
            h60_data = index_h60["data"]
            h60_centrals = index_h60["centrals"]
            h60_pens = index_h60["pens_effective"]
            h60_fractals = index_h60.get("fractals", [])

            first_sell, _ = _detect_first_sell_point(h60_data, h60_centrals, h60_pens, h60_fractals)
            second_sell, _ = _detect_second_sell_point(h60_data, h60_pens, h60_fractals)
            third_sell = _detect_third_sell_point(h60_data, h60_centrals, h60_pens, h60_fractals)
            h60_sell_triggered = first_sell or second_sell or third_sell
        except Exception:  # noqa: BLE001
            # 降级为简单笔方向切换检测
            pens_eff = index_h60["pens_effective"]
            if len(pens_eff) >= 2:
                h60_sell_triggered = (
                    pens_eff[-2]["direction"] == "up" and pens_eff[-1]["direction"] == "down"
                )

    # 三级风控判定（带容差）
    if daily_close < daily_azd - _EPS:
        result["state"] = "MARKET_DEAD"
        result["reason"] = "大盘已跌破战略底线 A-ZD，系统性风险爆发，强制清仓所有标的！"
    elif daily_close < daily_czd - _EPS or h60_sell_triggered:
        result["state"] = "MARKET_DANGER"
        if daily_close < daily_czd - _EPS:
            result["reason"] = "大盘跌破战术防线 C-ZD，今日严禁开新仓！"
        else:
            result["reason"] = "大盘60分钟触发卖点，今日严禁开新仓！"
    else:
        result["state"] = "MARKET_SAFE"
        result["reason"] = "大盘结构安全，个股可积极狙击！"

    return result


# ---------------------------------------------------------------------------
# 第三层：终极状态机与个股判定
# ---------------------------------------------------------------------------

def _classify_symbol_state(
    code: str,
    daily_result: Optional[Dict[str, Any]],
    h60_result: Optional[Dict[str, Any]],
    h15_result: Optional[Dict[str, Any]],
    holding_codes: set[str],
    market_state: str,
) -> Dict[str, Any]:
    """
    终极状态机判定（综合大盘风控 + 个股三维区间套），只能输出四种状态之一：

    优先级（从高到低）：
    1. 大盘 MARKET_DEAD          -> 强制 SELL（全局熔断）
    2. 个股跌破 A-ZD（死亡区）   -> IGNORE（拉黑，严禁抄底）
    3. 个股 60m+15m 顶背驰       -> SELL
    4. 个股 < C-ZD（走弱区）      -> SELL（持仓强制清仓）
    5. 持仓中 + 安全向上笔        -> HOLD
    6. 大盘 MARKET_DANGER        -> BUY 降级为 IGNORE
    7. 大盘 SAFE + 强势区 + 买点   -> BUY
    8. 其他                      -> IGNORE
    """
    state = "IGNORE"
    reason = "中枢震荡，无买卖点"

    daily_close: Optional[float] = None
    daily_czd: Optional[float] = None
    daily_azd: Optional[float] = None

    # 日线分析：提取 C-ZD / A-ZD
    if daily_result and daily_result.get("centrals") and daily_result.get("data"):
        daily_centrals = daily_result["centrals"]
        daily_data = daily_result["data"]
        if daily_centrals and daily_data:
            # 中枢列表按距离现价排序，必须按形成时间重新排序：最早为 A，最晚为 C
            sorted_centrals = sorted(
                daily_centrals,
                key=lambda c: c.get("form_end_date", c.get("end_date", "")),
            )
            daily_azd = float(sorted_centrals[0]["zd"])
            daily_czd = float(sorted_centrals[-1]["zd"])
            daily_close = float(daily_data[-1]["close"])

    # 60分钟分析
    h60_conditions = _compute_h60_conditions(
        h60_result.get("data", []) if h60_result else [],
        h60_result.get("centrals", []) if h60_result else [],
        h60_result.get("pens_effective", []) if h60_result else [],
    )

    # 15分钟分析
    h15_bottom_div = False
    h15_top_div = False
    if h15_result and h15_result.get("data") and h15_result.get("pens_effective"):
        h15_bottom_div = _detect_15m_bottom_divergence(
            h15_result["data"], h15_result["pens_effective"]
        )
        h15_top_div = _detect_15m_top_divergence(
            h15_result["data"], h15_result["pens_effective"]
        )

    is_holding = code in holding_codes

    # === 优先级 1：大盘 MARKET_DEAD ===
    # 持仓 -> SELL（强制清仓）；空仓 -> IGNORE（禁止开新仓）
    if market_state == "MARKET_DEAD":
        if is_holding:
            state = "SELL"
            reason = "大盘极度危险，强制清仓"
        else:
            state = "IGNORE"
            reason = "大盘极度危险，禁止开新仓"
    # === 优先级 2：持仓 + 跌破 A-ZD（死亡区）-> 强制 SELL ===
    elif is_holding and daily_close is not None and daily_azd is not None and daily_close < daily_azd - _EPS:
        state = "SELL"
        reason = "持仓跌破战略底线 A-ZD，强制清仓"
    # === 优先级 2.5：非持仓 + 跌破 A-ZD（死亡区）-> IGNORE ===
    elif daily_close is not None and daily_azd is not None and daily_close < daily_azd - _EPS:
        state = "IGNORE"
        reason = "跌破战略底线 A-ZD，拉黑"
    # === 优先级 3：持仓 + 60m+15m 顶背驰 -> SELL ===
    elif is_holding and h60_conditions["last_pen_up"] and h15_top_div:
        state = "SELL"
        reason = "60分钟向上笔+15分钟顶背驰"
    # === 优先级 3.5：非持仓 + 60m+15m 顶背驰 -> IGNORE ===
    elif h60_conditions["last_pen_up"] and h15_top_div:
        state = "IGNORE"
        reason = "60分钟向上笔+15分钟顶背驰，空仓不追"
    # === 优先级 4：持仓 + 跌破 C-ZD（走弱区）-> SELL ===
    elif is_holding and daily_close is not None and daily_czd is not None and daily_close < daily_czd - _EPS:
        state = "SELL"
        reason = "持仓跌破战术防线 C-ZD，清仓"
    # === 优先级 4.5：非持仓 + 跌破 C-ZD（走弱区）-> IGNORE ===
    elif daily_close is not None and daily_czd is not None and daily_close < daily_czd - _EPS:
        state = "IGNORE"
        reason = "跌破战术防线 C-ZD，放弃狙击"
    # === 优先级 5：持仓中 + 安全向上笔 -> HOLD ===
    elif is_holding and h60_conditions["last_pen_up"]:
        state = "HOLD"
        reason = "持仓中，安全向上笔"
    # === 优先级 5.5：持仓兜底保护（无明确卖点则继续持仓）===
    elif is_holding:
        state = "HOLD"
        reason = "持仓中，无明确卖点，继续观望"
    # === 优先级 6：大盘 DANGER -> BUY 降级为 IGNORE ===
    elif market_state == "MARKET_DANGER":
        state = "IGNORE"
        reason = "大盘警戒，禁止开新仓"
    # === 优先级 7：大盘 SAFE + 强势区 + 60m买点 + 15m底背驰 -> BUY ===
    elif (
        market_state == "MARKET_SAFE"
        and daily_close is not None
        and daily_czd is not None
        and daily_close >= daily_czd - _EPS
        and h60_conditions["in_c_central"]
        and h60_conditions["switched_down_to_up"]
        and h60_conditions["has_bottom_div_in_switch"]
        and h60_conditions["macd_buy"]
        and h15_bottom_div
    ):
        state = "BUY"
        reason = "多级别共振底背驰"
    else:
        state = "IGNORE"
        reason = "中枢震荡，无买卖点"

    return {
        "state": state,
        "reason": reason,
        "daily_close": daily_close,
        "daily_czd": daily_czd,
        "daily_azd": daily_azd,
        "h60_conditions": h60_conditions,
        "h15_bottom_div": h15_bottom_div,
        "h15_top_div": h15_top_div,
        "is_holding": is_holding,
    }


# ---------------------------------------------------------------------------
# 三维雷达自检构建
# ---------------------------------------------------------------------------

def _build_radar_checklist(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建三维雷达自检结果：
    - macro:  √/×  宏观(日线)：现价 vs C-ZD vs A-ZD
    - battle: √/×  战役(60分钟)
    - micro:  √/×  微观(15分钟)
    """
    daily_close = analysis.get("daily_close")
    daily_czd = analysis.get("daily_czd")
    daily_azd = analysis.get("daily_azd")
    h60_conditions = analysis.get("h60_conditions", {})
    h15_bottom_div = analysis.get("h15_bottom_div", False)
    h15_top_div = analysis.get("h15_top_div", False)

    # 宏观(日线)
    if daily_close is not None and daily_czd is not None and daily_azd is not None:
        macro_ok = daily_close >= daily_czd - _EPS
        if daily_close >= daily_czd - _EPS:
            macro_text = f"现价 {daily_close:.2f} 大于 战术防线 C-ZD {daily_czd:.2f} (战略底线 A-ZD: {daily_azd:.2f})"
        elif daily_close >= daily_azd - _EPS:
            macro_text = f"现价 {daily_close:.2f} 小于 战术防线 C-ZD {daily_czd:.2f}，但高于 战略底线 A-ZD {daily_azd:.2f}"
        else:
            macro_text = f"现价 {daily_close:.2f} 小于 战略底线 A-ZD {daily_azd:.2f} (C-ZD: {daily_czd:.2f})"
    else:
        macro_ok = False
        macro_text = "日线数据不足，无法判定"

    # 战役(60分钟)
    battle_ok = h60_conditions.get("in_c_central", False) or h60_conditions.get("last_pen_up", False)
    if h60_conditions.get("in_c_central"):
        battle_text = "回踩 ZD 支撑，当前处于 C 中枢内"
    elif h60_conditions.get("last_pen_up"):
        battle_text = "向上笔进行中，未触发卖点"
    elif h60_conditions.get("switched_up_to_down"):
        battle_text = "向上笔转向下笔，卖点触发"
    elif not h60_conditions.get("last_pen_up") and h60_conditions.get("switched_down_to_up") is False:
        battle_text = "向下笔进行中，动能向下"
    else:
        battle_text = "中枢震荡，无明确方向"

    # 微观(15分钟)
    if h15_bottom_div:
        micro_ok = True
        micro_text = "MACD 底背驰确认"
    elif h15_top_div:
        micro_ok = True
        micro_text = "MACD 顶背驰确认"
    else:
        micro_ok = False
        micro_text = "无背驰信号"

    return {
        "macro_ok": macro_ok,
        "macro_text": macro_text,
        "battle_ok": battle_ok,
        "battle_text": battle_text,
        "micro_ok": micro_ok,
        "micro_text": micro_text,
    }


# ---------------------------------------------------------------------------
# 军机处指令生成
# ---------------------------------------------------------------------------

def _generate_command(state: str, name: str, code: str, radar: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """根据状态生成包含 50,000 元的具体操作建议。"""
    daily_czd = analysis.get("daily_czd")

    if state == "SELL":
        if radar["micro_text"] == "MACD 顶背驰确认":
            return (
                f"🔴 警告：微观顶背驰确认，动能衰竭。立刻清仓，卖出 {FIXED_TRADE_AMOUNT:,} 元！"
            )
        return f"🔴 警告：宏观破位，趋势走坏。立刻清仓，卖出 {FIXED_TRADE_AMOUNT:,} 元！"

    if state == "BUY":
        stop_loss = f"绝对止损位设于 {daily_czd:.2f}" if daily_czd else "严格止损"
        return (
            f"🟢 狙击：大盘安全，多级别共振底背驰。建议满仓买入 {FIXED_TRADE_AMOUNT:,} 元，"
            f"{stop_loss}。"
        )

    if state == "HOLD":
        return f"🟡 观望：结构安全，动能充沛，持仓 {FIXED_TRADE_AMOUNT:,} 元不动。"

    return "⚪ 放弃：无明确信号或大盘风控拦截，空仓休息。"


# ---------------------------------------------------------------------------
# Markdown 报告追加写入
# ---------------------------------------------------------------------------

def _state_label(state: str) -> str:
    labels = {
        "SELL": "🔴 空仓警报",
        "BUY": "🟢 满仓突击",
        "HOLD": "🟡 持仓观望",
        "IGNORE": "⚪ 放弃狙击",
    }
    return labels.get(state, "⚪ 放弃狙击")


def _state_position(state: str) -> int:
    """返回当前持仓金额（硬编码规则）。SELL表示卖出后仓位为0。"""
    if state == "HOLD":
        return FIXED_TRADE_AMOUNT
    return 0


def _market_state_label(state: str) -> str:
    labels = {
        "MARKET_DEAD": "🔴 极度危险 (MARKET_DEAD)",
        "MARKET_DANGER": "🟡 警戒 (MARKET_DANGER)",
        "MARKET_SAFE": "🟢 安全 (MARKET_SAFE)",
    }
    return labels.get(state, "⚪ 未知")


def _append_trade_report(
    records: List[Dict[str, Any]],
    timestamp: datetime,
    market_info: Dict[str, Any],
) -> Path:
    """
    追加写入 Markdown 报告。
    - 目录：项目根目录 /trade_reports/
    - 文件名：作战指令_YYYY-MM-DD.md
    - 模式：追加 (a+)，绝不覆盖历史记录
    """
    TRADE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H:%M")
    path = TRADE_REPORT_DIR / f"作战指令_{date_str}.md"

    lines: List[str] = []

    # 标题
    lines.append(f"### ⏱️ 军机处巡航时间：{date_str} {time_str}")
    lines.append("")

    # ==================== 全局大盘风控 ====================
    market_price = market_info.get("price")
    market_czd = market_info.get("c_zd")
    market_azd = market_info.get("a_zd")
    market_state = market_info.get("state", "MARKET_SAFE")
    market_reason = market_info.get("reason", "")

    lines.append("#### 🌍 【全局大盘风控】")
    if market_price is not None and market_czd is not None and market_azd is not None:
        lines.append(
            f"- **上证指数 (000001.SH)**：现价 {market_price:.2f} (C-ZD: {market_czd:.2f}, A-ZD: {market_azd:.2f})"
        )
    else:
        lines.append("- **上证指数 (000001.SH)**：数据不足，无法判定大盘状态")
    lines.append(f"- **大盘状态**：{_market_state_label(market_state)}")
    lines.append(f"- **风控策略**：{market_reason}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ==================== 个股作战指令 ====================
    lines.append("#### 🛡️ 【个股作战指令】")
    lines.append("")

    # 分离有效区域和无效区域
    active_records = [r for r in records if r["state"] != "IGNORE"]
    ignore_records = [r for r in records if r["state"] == "IGNORE"]

    # 有效区域：完整输出
    for idx, rec in enumerate(active_records, start=1):
        radar = rec["radar"]
        state = rec["state"]
        pos = _state_position(state)
        lines.append(
            f"**{idx}. {rec['name']} ({rec['code']}) | 当前持仓：{pos:,} / {FIXED_TRADE_AMOUNT:,} 元**"
        )
        lines.append(f"- **【当前状态】**：{_state_label(state)}")
        lines.append("- **【三维雷达】**：")
        m_ok = "√" if radar["macro_ok"] else "×"
        b_ok = "√" if radar["battle_ok"] else "×"
        u_ok = "√" if radar["micro_ok"] else "×"
        lines.append(f"  - {m_ok} 宏观(日线)：{radar['macro_text']}")
        lines.append(f"  - {b_ok} 战役(60分钟)：{radar['battle_text']}")
        lines.append(f"  - {u_ok} 微观(15分钟)：{radar['micro_text']}")
        lines.append(f"- **【执行指令】**：{rec['command']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 无效区域：极简折叠输出（隐藏三维雷达）
    if ignore_records:
        lines.append("**以下标的放弃狙击，隐藏雷达以极简呈现：**")
        lines.append("")
        for rec in ignore_records:
            lines.append(f"- **{rec['name']} ({rec['code']})** | ⚪ 放弃狙击，空仓休息。")
        lines.append("")
        lines.append("---")
        lines.append("")

    content = "\n".join(lines) + "\n"

    with open(path, "a", encoding="utf-8") as f:
        f.write(content)

    return path


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_trade_command_engine() -> Path:
    """
    主入口：拉取 -> 计算 -> 判定 -> 写入报告 -> 返回文件路径。
    控制台仅打印一句：[SUCCESS] HH:mm 巡航完毕，报告已生成
    """
    # 延迟导入，避免循环依赖与启动时加载过重
    from services.indicators import get_index_kline

    timestamp = datetime.now()
    time_str = timestamp.strftime("%H:%M")

    symbols = _load_watchlist_observation_symbols()
    if not symbols:
        logging.warning("trade_command_engine: 监控池为空，跳过")
        TRADE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = TRADE_REPORT_DIR / f"作战指令_{timestamp.strftime('%Y-%m-%d')}.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"### ⏱️ 军机处巡航时间：{timestamp.strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write("_本次巡航监控池为空，无标的可分析。_\n\n---\n\n")
        print(f"[SUCCESS] {time_str} 巡航完毕，报告已生成")
        return path

    holding_codes = _load_holding_codes()
    daily_start = _daily_start_date()
    h60_start = _h60_start_date()
    h15_start = _h15_start_date()

    # ==================== 第一层：全局大盘风控 ====================
    index_daily: Optional[Dict[str, Any]] = None
    index_h60: Optional[Dict[str, Any]] = None

    try:
        index_daily = get_index_kline(
            symbol=INDEX_CODE,
            start_date=daily_start,
            end_date=None,
            period="daily",
            refresh=False,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("trade_command_engine: 大盘日线拉取失败: %s", e)

    try:
        index_h60 = get_index_kline(
            symbol=INDEX_CODE,
            start_date=h60_start,
            end_date=None,
            period="60",
            refresh=False,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("trade_command_engine: 大盘60m拉取失败: %s", e)

    market_info = _compute_market_state(index_daily, index_h60)
    market_state = market_info["state"]

    # ==================== 第二层：个股三维区间套 ====================
    records: List[Dict[str, Any]] = []

    for code, name in symbols:
        daily_result: Optional[Dict[str, Any]] = None
        h60_result: Optional[Dict[str, Any]] = None
        h15_result: Optional[Dict[str, Any]] = None

        # 静默拉取三周期数据（refresh=False，只读本地缓存）
        try:
            daily_result = get_index_kline(
                symbol=code,
                start_date=daily_start,
                end_date=None,
                period="daily",
                refresh=False,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("trade_command_engine: 日线拉取失败 %s: %s", code, e)

        try:
            h60_result = get_index_kline(
                symbol=code,
                start_date=h60_start,
                end_date=None,
                period="60",
                refresh=False,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("trade_command_engine: 60m拉取失败 %s: %s", code, e)

        try:
            h15_result = get_index_kline(
                symbol=code,
                start_date=h15_start,
                end_date=None,
                period="15",
                refresh=False,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("trade_command_engine: 15m拉取失败 %s: %s", code, e)

        # 终极状态机判定（单标异常不中断全量报告）
        try:
            analysis = _classify_symbol_state(
                code, daily_result, h60_result, h15_result, holding_codes, market_state
            )
            state = analysis["state"]
            radar = _build_radar_checklist(analysis)
            command = _generate_command(state, name, code, radar, analysis)
            records.append({
                "code": code,
                "name": name,
                "state": state,
                "radar": radar,
                "command": command,
            })
        except Exception as e:  # noqa: BLE001
            logging.warning("trade_command_engine: 标的 %s 分析失败: %s", code, e)

    # 按状态优先级排序：SELL > BUY > HOLD > IGNORE
    priority = {"SELL": 0, "BUY": 1, "HOLD": 2, "IGNORE": 3}
    records.sort(key=lambda r: priority.get(r["state"], 99))

    # ==================== 第四层：Markdown 报告生成 ====================
    path = _append_trade_report(records, timestamp, market_info)

    print(f"[SUCCESS] {time_str} 巡航完毕，报告已生成")
    return path
