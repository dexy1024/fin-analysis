"""
15分钟级状态机快照日志模块（CSV 中文版）

用途：
- 在每次15分钟巡检结束、状态机计算完毕后，将所有标的的状态追加写入 CSV。
- 支持 Excel 直接打开（utf-8-sig BOM 头）。
- 按年分文件：logs/snapshots_YYYY.csv
"""

import copy
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 项目根目录（backend/utils/ 的上两级）
ROOT_DIR = Path(__file__).resolve().parents[2]
LOGS_DIR = ROOT_DIR / "logs"

# CSV 表头（固定顺序，必须与 build_snapshot_data 输出键一致）
CSV_HEADERS = [
    "时间",
    "实际交易动作",
    "是否持仓",
    "大盘状态",
    "代码",
    "名称",
    "现价",
    "日线风控",
    "客观缠论信号",
    "60m笔方向",
    "15分信号",
    "决策理由",
    "日线A中枢ZD",
    "日线C中枢ZD",
    "锁定ZG",
    "15m_DIF",
    "15m_DEA",
    "底分型成立",
]

# 状态映射：英文 → 中文
_MARKET_STATE_MAP = {
    "MARKET_SAFE": "安全",
    "MARKET_DANGER": "警戒",
    "MARKET_DEAD": "极度危险",
}

_TRADE_SIGNAL_MAP = {
    "BUY_1": "一买",
    "BUY_2": "二买",
    "BUY_3": "三买",
    "SELL": "卖出",
    "HOLD": "持仓",
    "IGNORE": "观望",
}

# 卖点细分映射（优先级：一卖 > 二卖 > 三卖）
_SELL_PRIORITY = ["first_sell", "second_sell", "third_sell"]
_SELL_SIGNAL_MAP = {
    "first_sell": "一卖",
    "second_sell": "二卖",
    "third_sell": "三卖",
}

# 买点细分映射（优先级：二买 > 三买 > 一买，与状态机一致）
_BUY_PRIORITY = ["second_buy", "third_buy", "first_buy"]
_BUY_SIGNAL_MAP = {
    "first_buy": "一买",
    "second_buy": "二买",
    "third_buy": "三买",
}


def _ensure_logs_dir() -> Path:
    """确保日志目录存在。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def _check_is_holding(code: str) -> str:
    """
    检查标的是否在 watchlist.json 的 holdings 中。
    返回: "是" 或 "否"
    """
    try:
        watchlist_path = ROOT_DIR / "backend" / "data" / "watchlist.json"
        if watchlist_path.is_file():
            import json
            data = json.loads(watchlist_path.read_text(encoding="utf-8"))
            holdings = data.get("holdings", [])
            if any(str(item.get("code", "")).strip() == str(code).strip() for item in holdings):
                return "是"
    except Exception:
        pass
    return "否"


def _get_csv_path(timestamp: Optional[datetime] = None) -> Path:
    """按年分文件：logs/snapshots_YYYY.csv"""
    year = (timestamp or datetime.now()).strftime("%Y")
    return _ensure_logs_dir() / f"snapshots_{year}.csv"


def _to_chinese_market_state(state: str) -> str:
    return _MARKET_STATE_MAP.get(state, str(state))


def _to_chinese_trade_signal(
    state: str, sell_signals: Optional[Dict[str, bool]] = None, reason: str = "",
    chan_sig: str = "", pen_direction: str = ""
) -> str:
    """
    基于「客观缠论信号 + 60m笔方向」确定实际交易动作。

    全局规则：
    - 60m笔方向向下（寻底模式）：屏蔽所有卖点，只允许评估买点（一买/二买/三买）
    - 60m笔方向向上（冲顶模式）：屏蔽所有买点，只允许评估卖点（一卖/二卖/三卖）

    优先级：
    - 买点：二买 > 一买 > 三买
    - 卖点：二卖 > 一卖 > 三卖
    """
    # 检查信号组成
    has_first_buy = "一买" in chan_sig
    has_second_buy = "二买" in chan_sig
    has_third_buy = "三买" in chan_sig
    has_first_sell = "一卖" in chan_sig
    has_second_sell = "二卖" in chan_sig
    has_third_sell = "三卖" in chan_sig

    # 全局规则 1：60m笔方向向下（寻底模式）
    # 屏蔽所有卖点，只评估买点
    if pen_direction == "向下":
        if has_second_buy:
            return "二买"
        if has_first_buy:
            return "一买"
        if has_third_buy:
            return "三买"
        # 无买点时观望
        return "观望"

    # 全局规则 2：60m笔方向向上（冲顶模式）
    # 屏蔽所有买点，只评估卖点
    if pen_direction == "向上":
        if has_second_sell:
            return "二卖"
        if has_first_sell:
            return "一卖"
        if has_third_sell:
            return "三卖"
        # 无卖点时观望
        return "观望"

    # 兜底：根据状态机状态
    return _TRADE_SIGNAL_MAP.get(state, str(state))


def _fmt_float(value: Any) -> str:
    """将值格式化为两位小数字符串；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""


def _fmt_float4(value: Any) -> str:
    """将值格式化为四位小数字符串（用于 DIF/DEA）；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""


def _h15_signal(h15_result: Optional[Dict[str, Any]]) -> str:
    """
    独立计算15分钟背驰信号，仅依赖15分钟K线、笔和MACD数据。

    规则：
    - 底背驰：当前向下笔 + 价格创新低 + 动能衰竭(面积背驰或黄白线背离) + 底分型确认
    - 顶背驰：当前向上笔 + 价格创新高 + 动能衰竭(面积背驰或黄白线背离) + 顶分型确认
    - 不满足时返回 "无信号"
    """
    if not h15_result:
        return "无信号"

    data = h15_result.get("data", [])
    pens = h15_result.get("pens", [])
    pens_effective = h15_result.get("pens_effective", [])
    fractals = h15_result.get("fractals", [])

    if not data or len(data) < 3 or not pens_effective:
        return "无信号"

    # 构建日期到索引的映射
    date_to_idx = {item["date"]: i for i, item in enumerate(data)}

    # 获取有效笔（至少完成两根K线）
    effective_pens = [p for p in pens_effective if p.get("direction") in ("up", "down")]
    if len(effective_pens) < 2:
        return "无信号"

    # 当前笔：最新的一笔
    current_pen = effective_pens[-1]
    current_direction = current_pen.get("direction")

    # 同向对比笔：与当前笔方向相同的前一笔
    same_dir_pens = [p for p in effective_pens if p.get("direction") == current_direction]
    if len(same_dir_pens) < 2:
        return "无信号"
    compare_pen = same_dir_pens[-2]

    # 辅助函数：计算笔的MACD面积
    def calc_macd_area(pen: Dict[str, Any], is_green: bool) -> float:
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        area = 0.0
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None:
                if is_green and m < 0:
                    area += abs(m)
                elif not is_green and m > 0:
                    area += abs(m)
        return area

    # 辅助函数：计算笔内DIF极值
    def get_dif_extreme(pen: Dict[str, Any], find_max: bool) -> float:
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        dif_values = [
            item.get("macd", {}).get("dif")
            for item in data[s_idx:e_idx + 1]
            if item.get("macd", {}).get("dif") is not None
        ]
        if not dif_values:
            return 0.0
        return max(dif_values) if find_max else min(dif_values)

    # 辅助函数：检查笔末端是否有分型
    def has_fractal_at_end(pen: Dict[str, Any], fractal_type: str) -> bool:
        pen_end_date = pen.get("end_date")
        if not pen_end_date or not fractals:
            return False
        for f in fractals:
            if f.get("type") == fractal_type and f.get("date") == pen_end_date:
                return True
        return False

    # ========== 逻辑 1：底背驰判断 ==========
    if current_direction == "down":
        # 条件1：方向与空间 - 当前笔最低价 < 对比笔最低价（创新低）
        current_low = min(
            float(current_pen.get("start_price", 0)),
            float(current_pen.get("end_price", 0))
        )
        compare_low = min(
            float(compare_pen.get("start_price", 0)),
            float(compare_pen.get("end_price", 0))
        )
        if current_low < compare_low:
            # 条件2：动能衰竭（满足其一即可）
            # 面积背驰：当前绿柱面积 < 对比笔绿柱面积
            current_area = calc_macd_area(current_pen, is_green=True)
            compare_area = calc_macd_area(compare_pen, is_green=True)
            area_divergence = current_area > 0 and compare_area > 0 and current_area < compare_area

            # 黄白线背离：当前DIF最低值 > 对比笔DIF最低值
            current_dif_min = get_dif_extreme(current_pen, find_max=False)
            compare_dif_min = get_dif_extreme(compare_pen, find_max=False)
            dif_divergence = current_dif_min > compare_dif_min

            # 条件3：右侧确认 - 底分型
            has_bottom = has_fractal_at_end(current_pen, "bottom")

            if has_bottom and (area_divergence or dif_divergence):
                return "底背驰"

    # ========== 逻辑 2：顶背驰判断 ==========
    if current_direction == "up":
        # 条件1：方向与空间 - 当前笔最高价 > 对比笔最高价（创新高）
        current_high = max(
            float(current_pen.get("start_price", 0)),
            float(current_pen.get("end_price", 0))
        )
        compare_high = max(
            float(compare_pen.get("start_price", 0)),
            float(compare_pen.get("end_price", 0))
        )
        if current_high > compare_high:
            # 条件2：动能衰竭（满足其一即可）
            # 面积背驰：当前红柱面积 < 对比笔红柱面积
            current_area = calc_macd_area(current_pen, is_green=False)
            compare_area = calc_macd_area(compare_pen, is_green=False)
            area_divergence = current_area > 0 and compare_area > 0 and current_area < compare_area

            # 黄白线背离：当前DIF最高值 < 对比笔DIF最高值
            current_dif_max = get_dif_extreme(current_pen, find_max=True)
            compare_dif_max = get_dif_extreme(compare_pen, find_max=True)
            dif_divergence = current_dif_max < compare_dif_max

            # 条件3：右侧确认 - 顶分型
            has_top = has_fractal_at_end(current_pen, "top")

            if has_top and (area_divergence or dif_divergence):
                return "顶背驰"

    # ========== 逻辑 3：无信号 ==========
    return "无信号"


def _defense_detail(analysis: Dict[str, Any]) -> str:
    """
    防线偏离详情：计算现价与 min(A-ZD, C-ZD) 的偏离幅度。
    与状态机保持一致，优先使用 latest_close。
    """
    try:
        check_price = float(analysis.get("latest_close") or analysis.get("daily_close"))
        daily_azd = float(analysis.get("daily_azd"))
        daily_czd = float(analysis.get("daily_czd"))
        min_zd = min(daily_azd, daily_czd)
        if min_zd != 0:
            deviation = (check_price - min_zd) / min_zd * 100
            return f"min-ZD({min_zd:.2f})，现价{check_price:.2f}偏离{deviation:+.2f}%"
    except (TypeError, ValueError):
        pass
    return ""


def _core_reason(analysis: Dict[str, Any], market_state: str = "") -> str:
    """
    从状态机分析结果中提取风控驱动的核心原因（增强版）。
    匹配优先级（高→低）：跌破min-ZD > 顶背驰 > 买点确认
    增强内容：防线偏离幅度、级别对齐详情、笔方向。
    """
    state = analysis.get("state", "")
    reason = analysis.get("reason") or ""

    # 辅助：级别对齐详情
    def _align_detail() -> str:
        h15_align = analysis.get("h15_level_alignment")
        if h15_align:
            align_reason = getattr(h15_align, "reason", "")
            if align_reason:
                return align_reason
        return ""

    # 辅助：笔方向
    def _pen_dir() -> str:
        h60_conditions = analysis.get("h60_conditions") or {}
        return "向上笔" if h60_conditions.get("last_pen_up") else "向下笔"

    if "跌破战略底线" in reason or "跌破 min-ZD" in reason:
        detail = _defense_detail(analysis)
        base = "跌破战略底线，强制清仓" if state == "SELL" else "跌破战略底线，拉黑"
        return f"{base} | {detail}" if detail else base

    if "顶背驰" in reason:
        align = _align_detail()
        base = "60分钟向上笔+15分钟顶背驰"
        return f"{base} | {align}" if align else base

    if "一买确认" in reason or "二买确认" in reason or "三买确认" in reason:
        return reason

    if "无买卖点" in reason or "中枢震荡" in reason:
        pen_dir = _pen_dir()
        return f"{reason}，{pen_dir}" if pen_dir else reason

    if "持仓中" in reason:
        pen_dir = _pen_dir()
        return f"{reason}，{pen_dir}" if pen_dir else reason

    # 最终兜底
    return reason


def _chan_signal(
    buy_signals: Optional[Dict[str, bool]] = None,
    sell_signals: Optional[Dict[str, bool]] = None,
) -> str:
    """
    根据独立的买卖点检测结果生成纯缠论信号。
    不受状态机/风控影响，仅反映当前60分钟K线结构上的缠论买卖点。
    同时检测到多个信号时，用'+'连接显示所有信号。
    """
    signals: list[str] = []
    
    # 卖点信号（按优先级顺序）
    if sell_signals is not None:
        for key in _SELL_PRIORITY:
            if sell_signals.get(key):
                signals.append(_SELL_SIGNAL_MAP.get(key, ""))
    
    # 买点信号（按优先级顺序）
    if buy_signals is not None:
        for key in _BUY_PRIORITY:
            if buy_signals.get(key):
                signals.append(_BUY_SIGNAL_MAP.get(key, ""))
    
    return "+".join(signals) if signals else "无信号"


def _daily_risk_level(analysis: Dict[str, Any], price: Any = None) -> str:
    """
    根据现价与 MIN(A-ZD, C-ZD) 的关系映射日线风控状态。
    15分钟调度后，用最新价格（优先15分钟收盘价）与最低防线比较。
    """
    daily_czd = analysis.get("daily_czd")
    daily_azd = analysis.get("daily_azd")
    if daily_czd is None or daily_azd is None:
        return "安全"
    try:
        current_price = float(price) if price is not None else float(analysis.get("daily_close") or 0)
        czd = float(daily_czd)
        azd = float(daily_azd)
        min_zd = min(azd, czd)
        if current_price < min_zd:
            return "日线破位"
        return "安全"
    except (TypeError, ValueError):
        return "安全"


def _pen_direction(analysis: Dict[str, Any]) -> str:
    """60分钟最后一笔有效笔方向。"""
    h60_conditions = analysis.get("h60_conditions") or {}
    return "向上" if h60_conditions.get("last_pen_up") else "向下"


def _locked_zg(h60_result: Optional[Dict[str, Any]]) -> str:
    """60分钟最新中枢的 ZG（锁定ZG）。"""
    if not h60_result or not h60_result.get("centrals"):
        return ""
    try:
        sorted_c = sorted(
            h60_result["centrals"],
            key=lambda c: c.get("form_end_date") or c.get("end_date", ""),
        )
        return _fmt_float(sorted_c[-1].get("zg"))
    except Exception:
        return ""


def _h15_macd(h15_result: Optional[Dict[str, Any]]) -> tuple[str, str]:
    """15分钟最新K线的 DIF 与 DEA。"""
    if not h15_result or not h15_result.get("data"):
        return "", ""
    try:
        macd = h15_result["data"][-1].get("macd", {})
        dif = macd.get("dif")
        dea = macd.get("dea")
        return _fmt_float4(dif), _fmt_float4(dea)
    except Exception:
        return "", ""


def _has_bottom_fractal(h15_result: Optional[Dict[str, Any]]) -> str:
    """15分钟最近一根K线是否有底分型确认。"""
    if not h15_result or not h15_result.get("data") or not h15_result.get("fractals"):
        return "否"
    try:
        last_date = h15_result["data"][-1].get("date")
        if not last_date:
            return "否"
        for f in h15_result["fractals"]:
            if f.get("type") == "bottom" and f.get("date") == last_date:
                return "是"
        return "否"
    except Exception:
        return "否"


def _build_smart_reason(
    market_state: str,
    analysis: Dict[str, Any],
    chan_sig: str,
    h15_sig: str,
    trade_sig: str,
    pen_direction: str,
) -> str:
    """
    决策理由生成：说明缠论信号 + 60m笔方向 → 最终结论

    格式：客观缠论信号为{信号}，60分钟笔方向{向上/向下}，因此实际交易动作是{交易动作}
    """
    # 检查信号组成
    has_first_buy = "一买" in chan_sig
    has_second_buy = "二买" in chan_sig
    has_third_buy = "三买" in chan_sig
    has_first_sell = "一卖" in chan_sig
    has_second_sell = "二卖" in chan_sig
    has_third_sell = "三卖" in chan_sig

    # 构建信号描述
    signals = []
    if has_first_sell:
        signals.append("一卖")
    if has_second_sell:
        signals.append("二卖")
    if has_third_sell:
        signals.append("三卖")
    if has_first_buy:
        signals.append("一买")
    if has_second_buy:
        signals.append("二买")
    if has_third_buy:
        signals.append("三买")

    sig_desc = "、".join(signals) if signals else "无信号"

    # 根据笔方向和交易动作生成结论说明（使用实际的 pen_direction）
    mode_desc = "寻底模式" if pen_direction == "向下" else "冲顶模式"

    if trade_sig == "二买":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是二买（笔方向向下时优先买点）"

    if trade_sig == "二卖":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是二卖（笔方向向上时优先卖点）"

    if trade_sig == "一买":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是一买（趋势底背驰买入）"

    if trade_sig == "一卖":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是一卖（趋势顶背驰卖出）"

    if trade_sig == "三买":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是三买（突破回踩买入）"

    if trade_sig == "三卖":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}（{mode_desc}），因此实际交易动作是三卖（跌破反抽卖出）"

    if trade_sig == "持仓":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}，无明确买卖点，持仓观望"

    if trade_sig == "观望":
        return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}，中枢震荡，观望"

    if trade_sig == "风控卖出":
        return "跌破战略底线，触发风控，强制清仓"

    # 兜底
    return f"客观缠论信号为{sig_desc}，60分钟笔方向{pen_direction}，实际交易动作是{trade_sig}"


def build_snapshot_data(
    timestamp: datetime,
    code: str,
    name: str,
    market_state: str,
    analysis: Dict[str, Any],
    h60_result: Optional[Dict[str, Any]],
    h15_result: Optional[Dict[str, Any]],
    sell_signals: Optional[Dict[str, bool]] = None,
    buy_signals: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """
    将状态机分析结果拍平为 CSV 行字典。
    返回的字典中全为标量（字符串/数字），无内存引用污染。
    """
    analysis = copy.deepcopy(analysis)
    h60_result = copy.deepcopy(h60_result) if h60_result else None
    h15_result = copy.deepcopy(h15_result) if h15_result else None

    # 现价：直接使用状态机已计算好的 latest_close（优先级 15m > 60m > 日线），避免与决策逻辑分歧
    price = analysis.get("latest_close") or analysis.get("daily_close")

    dif, dea = _h15_macd(h15_result)

    chan_sig = _chan_signal(buy_signals, sell_signals)
    h15_sig = _h15_signal(h15_result)
    pen_dir = _pen_direction(analysis)
    trade_sig = _to_chinese_trade_signal(
        analysis.get("state", "IGNORE"),
        sell_signals,
        analysis.get("reason", ""),
        chan_sig,
        pen_dir,
    )
    smart_reason = _build_smart_reason(
        market_state, analysis, chan_sig, h15_sig, trade_sig, pen_dir
    )

    # 判断是否持仓（从 watchlist.json 的 holdings 中检查）
    is_holding = _check_is_holding(code)

    return {
        "时间": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "实际交易动作": trade_sig,
        "是否持仓": is_holding,
        "大盘状态": _to_chinese_market_state(market_state),
        "代码": str(code),
        "名称": str(name),
        "现价": _fmt_float(price),
        "日线风控": _daily_risk_level(analysis, price),
        "客观缠论信号": chan_sig,
        "60m笔方向": _pen_direction(analysis),
        "15分信号": h15_sig,
        "决策理由": smart_reason,
        "日线A中枢ZD": _fmt_float(analysis.get("daily_azd")),
        "日线C中枢ZD": _fmt_float(analysis.get("daily_czd")),
        "锁定ZG": _locked_zg(h60_result),
        "15m_DIF": dif,
        "15m_DEA": dea,
        "底分型成立": _has_bottom_fractal(h15_result),
    }


def _read_last_csv_time(path: Path) -> Optional[str]:
    """读取 CSV 最后一行的第一个字段（时间戳），通过 seek 到文件末尾避免全文件扫描。"""
    try:
        with open(path, "rb") as f:
            # 定位到文件末尾前 4KB（足够覆盖最后一行）
            f.seek(0, 2)
            file_size = f.tell()
            seek_pos = max(0, file_size - 4096)
            f.seek(seek_pos)
            # 如果是从文件中间开始读，先丢弃第一行（可能不完整）
            if seek_pos > 0:
                f.readline()
            lines = f.read().decode("utf-8-sig").splitlines()
            # 从后往前找第一个非空行
            for line in reversed(lines):
                line = line.strip()
                if line:
                    # 取第一个逗号前的内容即时间戳
                    return line.split(",")[0] if "," in line else line
    except Exception:
        logging.debug("csv_logger: 读取最后一行时间戳失败", exc_info=True)
    return None


def log_snapshot(data_dict: Dict[str, Any]) -> None:
    """
    将快照字典追加写入 CSV。文件不存在时自动写入表头。
    若检测到表头变更（字段增减），自动备份旧文件并重建新表头。
    时间戳变化时自动插入空行分隔，提升可读性。
    所有异常被静默捕获，绝不阻塞主交易逻辑。
    """
    try:
        time_str = data_dict.get("时间", "")
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S") if time_str else datetime.now()
        path = _get_csv_path(dt)
        file_exists = path.is_file()

        # 表头兼容性处理：已存在且非空、但表头不一致时备份旧文件
        if file_exists and path.stat().st_size > 0:
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.reader(f)
                    existing_headers = next(reader, [])
                if existing_headers and existing_headers != CSV_HEADERS:
                    backup_path = path.with_suffix(
                        f".csv.bak_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                    )
                    path.rename(backup_path)
                    file_exists = False
                    logging.info("csv_logger: 表头变更，已备份旧文件到 %s", backup_path)
            except Exception:
                logging.warning("csv_logger: 表头检查失败，跳过兼容性处理", exc_info=True)

        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            else:
                # 时间戳变化时插入空行分隔（跳过表头行）
                last_time = _read_last_csv_time(path)
                if last_time and last_time != time_str:
                    try:
                        datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
                        f.write("\r\n")  # 与 csv.writer 默认换行符保持一致
                    except ValueError:
                        pass  # last_time 是表头或其他非时间戳文本，跳过
            writer.writerow(data_dict)
    except Exception:
        logging.warning("csv_logger: 快照写入失败", exc_info=True)
