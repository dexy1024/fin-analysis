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

from services.chan_hourly_signals import (
    _detect_first_buy_point,
    _detect_first_sell_point,
    _detect_second_buy_point,
    _detect_second_sell_point,
    _detect_third_buy_point,
    _detect_third_sell_point,
)
from services.defense_radar import radar_output_dir
from services.first_buy_point import detect_first_buy_point
from services.indicators import get_index_kline
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

BUY_SELL_SIGNALS_JSON = "buy_sell_signals.json"

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _load_watchlist_observation_symbols() -> List[Tuple[str, str]]:
    """读取 watchlist.json、observation.json、observation_hk.json，返回 (code, name) 列表。"""
    from services.observation_data import load_watchlist_observation_symbols

    return load_watchlist_observation_symbols(include_hk=True)



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
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 获取 %s K线失败: %s", code, e)
        return has_buy, has_sell, details
    except Exception:
        logging.exception("buy_sell_signals: 获取 %s K线未预期异常", code)
        raise

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
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 一买检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 一买检测 %s 未预期异常", code)
        raise

    # 二买
    try:
        second_buy_has, second_buy_info = _detect_second_buy_point(data, pens_effective, fractals)
        if second_buy_has:
            details["second_buy"] = True
            has_buy = True
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 二买检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 二买检测 %s 未预期异常", code)
        raise

    # 三买
    try:
        third_buy_has, third_buy_info = _detect_third_buy_point(data, centrals, pens_effective, fractals)
        if third_buy_has:
            details["third_buy"] = True
            has_buy = True
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 三买检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 三买检测 %s 未预期异常", code)
        raise

    # 保存卖信号信息用于后续失效检查
    first_sell_info: Optional[Dict[str, Any]] = None
    second_sell_info: Optional[Dict[str, Any]] = None

    # 一卖
    try:
        first_sell_has, first_sell_info = _detect_first_sell_point(data, centrals, pens_effective, fractals)
        if first_sell_has:
            details["first_sell"] = True
            has_sell = True
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 一卖检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 一卖检测 %s 未预期异常", code)
        raise

    # 二卖
    try:
        second_sell_has, second_sell_info = _detect_second_sell_point(data, pens_effective, fractals)
        if second_sell_has:
            details["second_sell"] = True
            has_sell = True
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 二卖检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 二卖检测 %s 未预期异常", code)
        raise

    # 三卖
    try:
        if _detect_third_sell_point(data, centrals, pens_effective, fractals):
            details["third_sell"] = True
            has_sell = True
    except EXPECTED_BUSINESS_EXCEPTIONS as e:
        logging.debug("buy_sell_signals: 三卖检测 %s 失败: %s", code, e)
    except Exception:
        logging.exception("buy_sell_signals: 三卖检测 %s 未预期异常", code)
        raise

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
    except EXPECTED_BUSINESS_EXCEPTIONS:
        pass
    except Exception:
        logging.exception("buy_sell_signals: %s 日线支撑计算未预期异常", code)
        raise

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
    # 注：买点信号不做条件过滤，检测到什么就显示什么
    # 客观缠论信号应保持原始检测结果，与CSV和前端一致展示

    # 二买仅检查：回踩不创新低（与前端 hourlyBuySellSignals.ts 对齐）
    # 前端条件：retracementLow < cLow 才算创新低（等于不算）
    if details["second_buy"] and second_buy_info:
        buy1_stop = second_buy_info.get("buy1_stop")
        stop_loss_v = second_buy_info.get("stop_loss")
        if buy1_stop is not None and stop_loss_v is not None and float(stop_loss_v) < float(buy1_stop):
            details["second_buy"] = False

    # 三买仅检查：不在C中枢内（去除日线支撑过滤）
    if details["third_buy"] and in_c_central:
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
    # 注：卖点信号不做失效过滤，检测到什么就显示什么
    # 客观缠论信号应保持原始检测结果，与CSV和前端一致展示
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

    # 规则2（已移除）：二卖不再强制依赖一卖存在
    # 二卖检测函数内部已检查一卖c段顶分型，满足缠论定义即可
    # 客观缠论信号应保持独立检测结果

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
    except EXPECTED_BUSINESS_EXCEPTIONS:
        logging.warning("buy_sell_signals: 读取 %s 失败", path)
        return None
    except Exception:
        logging.exception("buy_sell_signals: 读取 %s 未预期异常", path)
        raise