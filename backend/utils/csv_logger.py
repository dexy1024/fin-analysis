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
    "代码",
    "名称",
    "现价",
    "大盘状态",
    "日线风控",
    "缠论信号",
    "15分信号",
    "交易信号",
    "决策理由",
    "60m笔方向",
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


def _get_csv_path(timestamp: Optional[datetime] = None) -> Path:
    """按年分文件：logs/snapshots_YYYY.csv"""
    year = (timestamp or datetime.now()).strftime("%Y")
    return _ensure_logs_dir() / f"snapshots_{year}.csv"


def _to_chinese_market_state(state: str) -> str:
    return _MARKET_STATE_MAP.get(state, str(state))


def _to_chinese_trade_signal(
    state: str, sell_signals: Optional[Dict[str, bool]] = None
) -> str:
    """
    将交易状态映射为可直接执行的操作指令。
    - SELL 时优先根据 sell_signals 细分为一卖/二卖/三卖。
    - 若无缠论卖点但 state == SELL，自动推断为风控驱动的「风控卖出」。
    """
    if state == "SELL" and sell_signals is not None:
        for key in _SELL_PRIORITY:
            if sell_signals.get(key):
                return _SELL_SIGNAL_MAP.get(key, "卖出")
        # 无缠论卖点但状态为 SELL，说明是风控驱动
        return "风控卖出"
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


def _h15_signal(analysis: Dict[str, Any]) -> str:
    """
    根据15分钟分析结果生成组合详情信号。
    组合条件：底背驰 / 顶背驰 / 趋势背驰 / 级别对齐
    用 '+' 连接多个同时成立的信号，无信号时返回 "无信号"。
    """
    signals: list[str] = []
    if analysis.get("h15_bottom_div"):
        signals.append("底背驰")
    if analysis.get("h15_top_div"):
        signals.append("顶背驰")
    h15_trend = analysis.get("h15_trend_div")
    if h15_trend and getattr(h15_trend, "has_signal", False):
        signals.append("趋势背驰")
    h15_align = analysis.get("h15_level_alignment")
    if h15_align and getattr(h15_align, "is_aligned", False):
        signals.append("级别对齐")
    return "+".join(signals) if signals else "无信号"


def _core_reason(analysis: Dict[str, Any], market_state: str = "") -> str:
    """
    从状态机分析结果中提取风控驱动的核心原因（简洁版）。
    匹配优先级（高→低）：大盘危险 > 跌破A-ZD > 跌破C-ZD > 顶背驰 > 买点确认
    若 reason 被降级覆盖导致丢失核心风控信息，从 market_state 兜底推断。
    """
    state = analysis.get("state", "")
    reason = analysis.get("reason") or ""
    if "大盘极度危险" in reason:
        return "大盘极度危险，强制清仓" if state == "SELL" else "大盘极度危险，禁止开新仓"
    if "跌破战略底线" in reason or "跌破 A-ZD" in reason:
        return "跌破战略底线 A-ZD，强制清仓" if state == "SELL" else "跌破战略底线 A-ZD，拉黑"
    if "跌破战术防线" in reason or "跌破 C-ZD" in reason:
        return "跌破战术防线 C-ZD，清仓" if state == "SELL" else "跌破战术防线 C-ZD，放弃狙击"
    if "顶背驰" in reason:
        return "60分钟向上笔+15分钟顶背驰"
    if "一买确认" in reason or "二买确认" in reason or "三买确认" in reason:
        return reason
    if "无买卖点" in reason or "中枢震荡" in reason:
        # 兜底：若 reason 被降级覆盖但 market_state 为 DEAD/DANGER，优先展示风控原因
        if market_state == "MARKET_DEAD":
            return "大盘极度危险，强制清仓" if state == "SELL" else "大盘极度危险，禁止开新仓"
        if market_state == "MARKET_DANGER":
            if state == "SELL":
                return "大盘警戒，强制清仓"
            if state == "HOLD":
                return "大盘警戒，持仓观望"
            return "大盘警戒，禁止开新仓"
        return reason
    if "持仓中" in reason:
        return reason
    if "大盘警戒" in reason:
        return reason
    # 最终兜底
    if market_state == "MARKET_DEAD":
        return "大盘极度危险，强制清仓" if state == "SELL" else "大盘极度危险，禁止开新仓"
    if market_state == "MARKET_DANGER":
        if state == "SELL":
            return "大盘警戒，强制清仓"
        if state == "HOLD":
            return "大盘警戒，持仓观望"
        return "大盘警戒，禁止开新仓"
    return reason


def _chan_signal(
    buy_signals: Optional[Dict[str, bool]] = None,
    sell_signals: Optional[Dict[str, bool]] = None,
) -> str:
    """
    根据独立的买卖点检测结果生成纯缠论信号。
    不受状态机/风控影响，仅反映当前60分钟K线结构上的缠论买卖点。
    卖点优先级：一卖 > 二卖 > 三卖
    买点优先级：二买 > 三买 > 一买
    """
    if sell_signals is not None:
        for key in _SELL_PRIORITY:
            if sell_signals.get(key):
                return _SELL_SIGNAL_MAP.get(key, "")
    if buy_signals is not None:
        for key in _BUY_PRIORITY:
            if buy_signals.get(key):
                return _BUY_SIGNAL_MAP.get(key, "")
    return "无信号"


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
    h60_conditions = analysis.get("h60_conditions", {})
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


def _build_structured_reason(
    market_state: str,
    analysis: Dict[str, Any],
    chan_sig: str,
    h15_sig: str,
    trade_sig: str,
) -> str:
    """
    基于四个维度生成结构化决策理由。
    格式：【大盘】{大盘状态} | 【日线】{日线风控} | 【缠论】{缠论信号} | 【15分】{15分信号} → {交易信号}（{核心原因}）
    """
    daily_risk = _daily_risk_level(analysis)
    core = _core_reason(analysis, market_state)
    market_cn = _to_chinese_market_state(market_state)
    return f"【大盘】{market_cn} | 【日线】{daily_risk} | 【缠论】{chan_sig} | 【15分】{h15_sig} → {trade_sig}（{core}）"


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

    # 现价：优先取15分钟最后一根收盘价，其次日线收盘价
    price = analysis.get("daily_close")
    if h15_result and h15_result.get("data"):
        try:
            price = h15_result["data"][-1].get("close", price)
        except Exception:
            pass

    dif, dea = _h15_macd(h15_result)

    chan_sig = _chan_signal(buy_signals, sell_signals)
    h15_sig = _h15_signal(analysis)
    trade_sig = _to_chinese_trade_signal(analysis.get("state", "IGNORE"), sell_signals)
    structured_reason = _build_structured_reason(
        market_state, analysis, chan_sig, h15_sig, trade_sig
    )

    return {
        "时间": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "代码": str(code),
        "名称": str(name),
        "现价": _fmt_float(price),
        "大盘状态": _to_chinese_market_state(market_state),
        "日线风控": _daily_risk_level(analysis, price),
        "缠论信号": chan_sig,
        "15分信号": h15_sig,
        "交易信号": trade_sig,
        "决策理由": structured_reason,
        "60m笔方向": _pen_direction(analysis),
        "日线A中枢ZD": _fmt_float(analysis.get("daily_azd")),
        "日线C中枢ZD": _fmt_float(analysis.get("daily_czd")),
        "锁定ZG": _locked_zg(h60_result),
        "15m_DIF": dif,
        "15m_DEA": dea,
        "底分型成立": _has_bottom_fractal(h15_result),
    }


def log_snapshot(data_dict: Dict[str, Any]) -> None:
    """
    将快照字典追加写入 CSV。文件不存在时自动写入表头。
    若检测到表头变更（字段增减），自动备份旧文件并重建新表头。
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
            writer.writerow(data_dict)
    except Exception:
        logging.warning("csv_logger: 快照写入失败", exc_info=True)
