"""
无头（Headless）多级别缠论量化黑盒报告引擎

核心架构：
- 双轨并行：独立后台脚本，绝对禁止触碰前端 UI
- 极速算力：各级别数据 limit 锁定约 250 根 K 线，算完即释放内存
- 资金铁律：所有交易指令硬编码基于 50,000 元人民币满仓额度

三层体系：
1. 全局大盘状态（上证指数 000001.SH）：MARKET_DEAD / MARKET_DANGER / MARKET_SAFE
   （仅作参考展示，不再驱动个股交易决策）
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
    """持仓识别唯一权威来源：watchlist.json 的 holdings 列表。"""
    watchlist_path = ROOT_DIR / "backend" / "data" / "watchlist.json"
    if not watchlist_path.is_file():
        return set()
    try:
        data = json.loads(watchlist_path.read_text(encoding="utf-8"))
        return {
            str(item["code"]).strip()
            for item in data.get("holdings", [])
            if isinstance(item, dict) and item.get("code")
        }
    except Exception:  # noqa: BLE001
        logging.warning("trade_command_engine: 读取 watchlist.json 失败")
        return set()


def _load_holding_amounts() -> Dict[str, int]:
    """
    返回 watchlist 中各标的的持仓金额(元)。
    金额优先级：positions.json(真实交易记录) > watchlist.json(shares*cost) > 默认 10,000
    """
    # 1. 先拿到 watchlist 中的持仓 code 列表（唯一持仓来源）
    watchlist_path = ROOT_DIR / "backend" / "data" / "watchlist.json"
    holding_amounts: Dict[str, int] = {}
    if not watchlist_path.is_file():
        return holding_amounts

    try:
        data = json.loads(watchlist_path.read_text(encoding="utf-8"))
        for item in data.get("holdings", []):
            if isinstance(item, dict) and item.get("code"):
                code = str(item["code"]).strip()
                shares = item.get("shares")
                cost = item.get("cost")
                if shares is not None and cost is not None:
                    holding_amounts[code] = int(float(shares) * float(cost))
                else:
                    holding_amounts[code] = 10_000  # 默认仓位
    except Exception:  # noqa: BLE001
        logging.warning("trade_command_engine: 读取 watchlist.json 失败")
        return {}

    # 2. 用 positions.json 中的真实金额覆盖（如有）
    positions_file = ROOT_DIR / "data" / "positions.json"
    if positions_file.is_file():
        try:
            with open(positions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for p in data:
                if isinstance(p, dict) and p.get("status") == "holding":
                    code = str(p["code"]).strip()
                    # 只在 watchlist 中的标才覆盖金额
                    if code in holding_amounts:
                        amount = int(float(p.get("amount", 0) or 0))
                        if amount > 0:
                            holding_amounts[code] = amount
        except Exception:  # noqa: BLE001
            logging.warning("trade_command_engine: 读取 positions.json 金额失败")

    return holding_amounts


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
# 15分钟趋势背驰显式检测（含跨级别联立校验）
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class TrendDivergenceResult:
    """15分钟趋势背驰检测结果"""
    has_signal: bool = False
    divergence_type: str = ""  # "trend" 或 "pan"
    div_date: str = ""
    div_price: float = 0.0
    b_area: float = 0.0
    c_area: float = 0.0
    area_ratio: float = 0.0
    hub_count: int = 0
    hub_b_low: float = 0.0
    current_low: float = 0.0
    reasons: str = ""


@dataclass
class LevelAlignmentResult:
    """跨级别联立校验结果：15分钟背驰 ↔ 60分钟笔完成状态"""
    is_aligned: bool = False
    reason: str = ""
    h60_pen_state: str = ""  # "down_complete", "up_ongoing", "down_ongoing", "up_complete"


def _find_downward_hubs(
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    识别向下中枢（进入段为向下笔 + 内部三笔不完全同向）。

    ⚠️ 重要：_build_centrals 使用的是 pens_effective，所以 segment_indices
    对应的是 pens_effective 的索引，而非原始 pens。
    """
    downward_hubs = []
    for central in centrals:
        idxs = central.get("segment_indices", [])
        if len(idxs) != 3:
            continue
        p1 = pens_effective[idxs[0]] if idxs[0] < len(pens_effective) else None
        p2 = pens_effective[idxs[1]] if idxs[1] < len(pens_effective) else None
        p3 = pens_effective[idxs[2]] if idxs[2] < len(pens_effective) else None
        if p1 is None or p2 is None or p3 is None:
            continue
        if p1.get("direction") != "down":
            continue
        dirs = [p1.get("direction"), p2.get("direction"), p3.get("direction")]
        if len(set(dirs)) == 1:
            continue
        downward_hubs.append({
            "start_date": central.get("start_date"),
            "end_date": central.get("end_date"),
            "zg": central.get("zg"),
            "zd": central.get("zd"),
        })
    downward_hubs.sort(key=lambda x: x["start_date"])
    return downward_hubs


def _macd_green_area(
    data: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
) -> float:
    """计算指定区间MACD绿柱总面积"""
    area = 0.0
    in_range = False
    for item in data:
        date = item.get("date", "")
        if date == start_date:
            in_range = True
        if in_range:
            macd = item.get("macd", {}).get("macd", 0)
            if macd is not None and macd < 0:
                area += abs(macd)
        if date == end_date:
            break
    return area


def _macd_retraced_zero(
    data: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
) -> bool:
    """检查区间MACD是否回抽零轴（DIF>=0 或 MACD柱>=0）"""
    in_range = False
    for item in data:
        date = item.get("date", "")
        if date == start_date:
            in_range = True
        if in_range:
            macd = item.get("macd", {})
            m = macd.get("macd", 0)
            dif = macd.get("dif", 0)
            if m is not None and m >= 0:
                return True
            if dif is not None and dif >= 0:
                return True
        if date == end_date:
            break
    return False


def _has_bottom_fractal_at_date(
    fractals: List[Dict[str, Any]],
    date: str,
) -> bool:
    """检查指定日期是否有底分型"""
    return any(
        f.get("type") == "bottom" and f.get("date") == date
        for f in fractals
    )


def _detect_15m_trend_bottom_divergence(
    data: List[Dict[str, Any]],
    centrals: List[Dict[str, Any]],
    pens_effective: List[Dict[str, Any]],
    fractals: List[Dict[str, Any]],
) -> TrendDivergenceResult:
    """
    15分钟趋势底背驰显式检测。

    趋势背驰定义（a+A+b+B+c 结构）：
    1. 至少2个向下中枢（A、B中枢）
    2. B中枢构建期间MACD回抽零轴
    3. c段向下笔创新低（跌破B中枢低点ZD）
    4. c段MACD绿柱面积 < b段面积（背驰）
    5. c段终点出现底分型

    盘整背驰定义（a+A+b 结构）：
    1. 仅1个向下中枢（A中枢）
    2. b段向下笔跌破A中枢低点
    3. b段MACD面积 < a段面积
    4. b段终点底分型
    """
    empty = TrendDivergenceResult()

    if not data or len(data) < 10 or not centrals or not pens_effective:
        return empty

    # 1. 识别向下中枢（segment_indices 对应 pens_effective）
    downward_hubs = _find_downward_hubs(centrals, pens_effective)
    if not downward_hubs:
        return empty

    # 获取所有向下笔（按时间排序）——使用 pens_effective
    down_pens = [p for p in pens_effective if p.get("direction") == "down"]
    down_pens.sort(key=lambda x: x.get("start_date", ""))
    if len(down_pens) < 2:
        return empty

    # ============================================================
    # 分支A：趋势背驰（>=2个向下中枢）
    # ============================================================
    if len(downward_hubs) >= 2:
        hub_a = downward_hubs[-2]
        hub_b = downward_hubs[-1]

        # 找到B中枢后的向下笔（c段）：结束在B中枢之后 + 创新低
        hub_b_end = hub_b["end_date"]
        hub_b_low = float(hub_b["zd"] or 0)
        c_pen = None
        for pen in reversed(down_pens):
            pen_end = pen.get("end_date")
            pen_low = min(pen.get("start_price", 0), pen.get("end_price", 0))
            if pen_end and hub_b_end and pen_end > hub_b_end and pen_low < hub_b_low:
                c_pen = pen
                break

        if c_pen:
            # 找到b段：在A中枢结束后、B中枢开始前（或内）、c段前的向下笔
            hub_a_end = hub_a["end_date"]
            b_pen = None
            for pen in reversed(down_pens):
                pen_end = pen.get("end_date")
                if (pen_end and hub_a_end and pen_end > hub_a_end
                        and pen_end < c_pen.get("start_date", "")):
                    b_pen = pen
                    break

            if b_pen:
                # c段创新低检查
                c_low = min(c_pen.get("start_price", 0), c_pen.get("end_price", 0))
                if c_low < hub_b_low:
                    # MACD回抽零轴
                    macd_ok = _macd_retraced_zero(data, hub_b["start_date"], hub_b["end_date"])

                    # 计算面积
                    b_area = _macd_green_area(data, b_pen.get("start_date", ""), b_pen.get("end_date", ""))
                    c_area = _macd_green_area(data, c_pen.get("start_date", ""), c_pen.get("end_date", ""))

                    if macd_ok and b_area > 0 and c_area > 0 and c_area < b_area:
                        # 底分型确认
                        c_end_date = c_pen.get("end_date", "")
                        if _has_bottom_fractal_at_date(fractals, c_end_date):
                            return TrendDivergenceResult(
                                has_signal=True,
                                divergence_type="trend",
                                div_date=c_end_date,
                                div_price=c_pen.get("end_price", 0),
                                b_area=b_area,
                                c_area=c_area,
                                area_ratio=c_area / b_area,
                                hub_count=len(downward_hubs),
                                hub_b_low=hub_b_low,
                                current_low=c_low,
                                reasons=(
                                    f"趋势背驰: c段面积({c_area:.3f}) < b段面积({b_area:.3f}), "
                                    f"c_low={c_low:.2f} < hub_b_low={hub_b_low:.2f}"
                                ),
                            )

    # ============================================================
    # 分支B：盘整背驰（仅1个向下中枢）
    # ============================================================
    if len(downward_hubs) >= 1:
        hub_a = downward_hubs[-1]
        hub_a_end = hub_a["end_date"]
        hub_a_low = float(hub_a["zd"] or 0)

        # 找到中枢后的向下笔（b段）
        b_pen = None
        for pen in reversed(down_pens):
            pen_end = pen.get("end_date")
            pen_low = min(pen.get("start_price", 0), pen.get("end_price", 0))
            if pen_end and hub_a_end and pen_end > hub_a_end and pen_low < hub_a_low:
                b_pen = pen
                break

        if b_pen:
            # 找到进入段a：中枢前的向下笔
            hub_a_start = hub_a["start_date"]
            a_pen = None
            for pen in reversed(down_pens):
                pen_end = pen.get("end_date")
                if pen_end and pen_end < hub_a_start:
                    a_pen = pen
                    break

            if a_pen:
                a_area = _macd_green_area(data, a_pen.get("start_date", ""), a_pen.get("end_date", ""))
                b_area = _macd_green_area(data, b_pen.get("start_date", ""), b_pen.get("end_date", ""))

                if a_area > 0 and b_area > 0 and b_area < a_area:
                    b_end_date = b_pen.get("end_date", "")
                    if _has_bottom_fractal_at_date(fractals, b_end_date):
                        return TrendDivergenceResult(
                            has_signal=True,
                            divergence_type="pan",
                            div_date=b_end_date,
                            div_price=b_pen.get("end_price", 0),
                            b_area=b_area,
                            c_area=a_area,  # 这里a_area对应进入段
                            area_ratio=b_area / a_area,
                            hub_count=len(downward_hubs),
                            hub_b_low=hub_a_low,
                            current_low=min(b_pen.get("start_price", 0), b_pen.get("end_price", 0)),
                            reasons=(
                                f"盘整背驰: b段面积({b_area:.3f}) < a段面积({a_area:.3f}), "
                                f"b_low={min(b_pen.get('start_price',0), b_pen.get('end_price',0)):.2f} < hub_low={hub_a_low:.2f}"
                            ),
                        )

    return empty


def _check_level_alignment_15m_to_60m(
    trend_div: TrendDivergenceResult,
    h60_pens_effective: List[Dict[str, Any]],
) -> LevelAlignmentResult:
    """
    跨级别联立校验：15分钟趋势背驰时，60分钟笔必须处于完成节点。

    缠论原理：
    - 15分钟一个走势类型的结束（趋势背驰） ↔ 60分钟一笔的完成
    - 如果15分钟出现底背驰，60分钟的向下笔必须已经结束，且最好已开始向上笔

    校验规则：
    1. 15分钟无底背驰 → 无需校验，返回 aligned=True
    2. 15分钟有底背驰 + 60分钟最后两笔为"前下后上" → aligned=True（完全同步）
    3. 15分钟有底背驰 + 60分钟最后一笔为向下且刚结束 → aligned=True（笔刚完成）
    4. 15分钟有底背驰 + 60分钟最后一笔为向下进行中 → aligned=False（级别不同步！）
    5. 15分钟有底背驰 + 60分钟最后一笔为向上但已开始很久 → 警告（可能错过最佳买点）
    """
    if not trend_div.has_signal:
        return LevelAlignmentResult(
            is_aligned=True,
            reason="15分钟无趋势底背驰，无需校验级别联立",
            h60_pen_state="unknown",
        )

    if not h60_pens_effective or len(h60_pens_effective) < 2:
        return LevelAlignmentResult(
            is_aligned=False,
            reason="60分钟笔数据不足，无法校验级别联立",
            h60_pen_state="unknown",
        )

    prev_pen = h60_pens_effective[-2]
    curr_pen = h60_pens_effective[-1]
    prev_dir = prev_pen.get("direction", "")
    curr_dir = curr_pen.get("direction", "")

    # 完全同步：60分钟前下后上
    if prev_dir == "down" and curr_dir == "up":
        return LevelAlignmentResult(
            is_aligned=True,
            reason="✅ 级别完全同步：15分钟底背驰 ↔ 60分钟向下笔已完成、向上笔已开始",
            h60_pen_state="down_complete",
        )

    # 60分钟最后一笔向下：需要判断是否已结束
    if curr_dir == "down":
        # 15分钟背驰点日期
        div_date = trend_div.div_date
        curr_end = curr_pen.get("end_date", "")
        # 如果15分钟背驰点在60分钟向下笔结束之后（或相同），说明60分钟笔已结束
        if div_date and curr_end and div_date >= curr_end:
            return LevelAlignmentResult(
                is_aligned=True,
                reason="✅ 级别同步：15分钟底背驰点落在60分钟向下笔结束之后（笔已完成）",
                h60_pen_state="down_complete",
            )
        return LevelAlignmentResult(
            is_aligned=False,
            reason="❌ 级别不同步！15分钟底背驰但60分钟向下笔仍在进行中（未结束）",
            h60_pen_state="down_ongoing",
        )

    # 60分钟最后一笔向上，但前笔也是向上（无向下笔过渡）
    if curr_dir == "up" and prev_dir == "up":
        return LevelAlignmentResult(
            is_aligned=False,
            reason="⚠️ 级别可能不同步：60分钟连续向上笔，无向下笔完成记录",
            h60_pen_state="up_ongoing_no_retracement",
        )

    # 其他情况
    return LevelAlignmentResult(
        is_aligned=False,
        reason=f"⚠️ 级别状态不明：60分钟笔状态为 前笔={prev_dir}, 当前={curr_dir}",
        h60_pen_state=f"{prev_dir}_{curr_dir}",
    )


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
        "macd_sell": False,
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
            macd_red_shrink = m0 > 0 and m1 > 0 and m0 < m1

            result["macd_buy"] = (
                (macd_green_short or macd_green_to_red or macd_red_len)
                and (dif0 > dif1 or (dif1 <= dea1 and dif0 > dea0))
                and not (m0 < 0 and m1 < 0 and m2 < 0 and abs(m0) > abs(m1) and abs(m1) > abs(m2))
            )
            result["macd_sell"] = macd_red_shrink

    return result


# ---------------------------------------------------------------------------
# 第一层：全局大盘风控总闸
# ---------------------------------------------------------------------------

def _compute_market_state(
    index_daily: Optional[Dict[str, Any]],
    index_h60: Optional[Dict[str, Any]],
    index_h15: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    计算上证指数全局状态，返回三级参考状态之一：
    - MARKET_DEAD:   现价 < A-ZD（极度危险）
    - MARKET_DANGER: A-ZD <= 现价 < C-ZD，或 60m/15m 触发卖点（警戒）
    - MARKET_SAFE:   现价 >= C-ZD 且 60m/15m 无卖点（安全）
    注：大盘状态仅作参考展示，个股交易决策由自身缠论信号和防线状态独立驱动。
    """
    # 故障安全原则：数据缺失时默认保守参考状态（警戒）
    result = {
        "state": "MARKET_DANGER",
        "price": None,
        "c_zd": None,
        "a_zd": None,
        "reason": "大盘数据不足，默认进入警戒状态",
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

    # 优先使用更高频的最新价格做风控判断（15m > 60m > 日线）
    latest_close = daily_close
    if index_h60 and index_h60.get("data"):
        try:
            h60_close = float(index_h60["data"][-1]["close"])
            if h60_close > 0:
                latest_close = h60_close
        except Exception:
            pass
    if index_h15 and index_h15.get("data"):
        try:
            h15_close = float(index_h15["data"][-1]["close"])
            if h15_close > 0:
                latest_close = h15_close
        except Exception:
            pass

    result["price"] = latest_close
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

    # 15分钟卖点检测：与60分钟卖点并列，增加灵敏度
    h15_sell_triggered = False
    if index_h15 and index_h15.get("data") and index_h15.get("centrals") and index_h15.get("pens_effective"):
        try:
            from services.buy_sell_signals import (
                _detect_first_sell_point,
                _detect_second_sell_point,
                _detect_third_sell_point,
            )
            h15_data = index_h15["data"]
            h15_centrals = index_h15["centrals"]
            h15_pens = index_h15["pens_effective"]
            h15_fractals = index_h15.get("fractals", [])

            first_sell, _ = _detect_first_sell_point(h15_data, h15_centrals, h15_pens, h15_fractals)
            second_sell, _ = _detect_second_sell_point(h15_data, h15_pens, h15_fractals)
            third_sell = _detect_third_sell_point(h15_data, h15_centrals, h15_pens, h15_fractals)
            h15_sell_triggered = first_sell or second_sell or third_sell
        except Exception:  # noqa: BLE001
            pens_eff = index_h15["pens_effective"]
            if len(pens_eff) >= 2:
                h15_sell_triggered = (
                    pens_eff[-2]["direction"] == "up" and pens_eff[-1]["direction"] == "down"
                )

    # 三级风控判定（带容差）：以 min(A-ZD, C-ZD) 为战略底线，max 为战术防线
    min_zd = min(daily_azd, daily_czd)
    max_zd = max(daily_azd, daily_czd)

    if latest_close < min_zd - _EPS:
        result["state"] = "MARKET_DEAD"
        result["reason"] = "大盘已跌破战略底线，系统性风险爆发，强制清仓所有标的！"
    elif latest_close < max_zd - _EPS or h60_sell_triggered or h15_sell_triggered:
        result["state"] = "MARKET_DANGER"
        if latest_close < max_zd - _EPS:
            result["reason"] = "大盘跌破战术防线，今日严禁开新仓！"
        elif h60_sell_triggered:
            result["reason"] = "大盘60分钟触发卖点，今日严禁开新仓！"
        else:
            result["reason"] = "大盘15分钟触发卖点，今日严禁开新仓！"
    else:
        result["state"] = "MARKET_SAFE"
        result["reason"] = "大盘结构安全，个股可积极狙击！"

    return result


def _build_buy_hint_for_holding(
    second_buy: bool, third_buy: bool, first_buy: bool, h15_top_div: bool = False
) -> str:
    """
    为持仓标的构建买点提示文案。
    优先级：二买 > 三买 > 一买
    15分钟顶背驰时，抑制"可加仓"提示，改为观望等待。
    """
    if second_buy:
        return "二买信号（等待15分回调结束）" if h15_top_div else "当前存在二买信号（可加仓）"
    if third_buy:
        return "三买信号（等待15分回调结束）" if h15_top_div else "当前存在三买信号（可加仓）"
    if first_buy:
        return "当前存在一买信号（左侧试探）"
    return ""


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
    三维共振状态机（日线 → 60m → 15m），输出细分状态：
    SELL / BUY_1 / BUY_2 / BUY_3 / HOLD / IGNORE
    """
    state = "IGNORE"
    reason = "中枢震荡，无买卖点"
    h60_buy_type: Optional[str] = None

    daily_close: Optional[float] = None
    daily_czd: Optional[float] = None
    daily_azd: Optional[float] = None

    # 日线分析：提取 C-ZD / A-ZD
    if daily_result and daily_result.get("centrals") and daily_result.get("data"):
        daily_centrals = daily_result["centrals"]
        daily_data = daily_result["data"]
        if daily_centrals and daily_data:
            sorted_centrals = sorted(
                daily_centrals,
                key=lambda c: c.get("form_end_date", c.get("end_date", "")),
            )
            daily_azd = float(sorted_centrals[0]["zd"])
            daily_czd = float(sorted_centrals[-1]["zd"])
            daily_close = float(daily_data[-1]["close"])

    # 优先使用更高频的最新价格做风控判断（15m > 60m > 日线），与CSV保持一致
    latest_close = daily_close
    if h60_result and h60_result.get("data"):
        try:
            h60_close = float(h60_result["data"][-1]["close"])
            if h60_close > 0:
                latest_close = h60_close
        except Exception:
            pass
    if h15_result and h15_result.get("data"):
        try:
            h15_close = float(h15_result["data"][-1]["close"])
            if h15_close > 0:
                latest_close = h15_close
        except Exception:
            pass

    # 个股风控阈值与大盘统一：min(A-ZD, C-ZD) 为防线基准
    min_zd = min(daily_azd, daily_czd) if daily_azd is not None and daily_czd is not None else None

    # 60分钟分析
    h60_conditions = _compute_h60_conditions(
        h60_result.get("data", []) if h60_result else [],
        h60_result.get("centrals", []) if h60_result else [],
        h60_result.get("pens_effective", []) if h60_result else [],
    )

    # 60分钟买卖点检测（区分一买/二买/三买）
    h60_first_buy, h60_first_buy_info = False, None
    h60_second_buy, h60_second_buy_info = False, None
    h60_third_buy, h60_third_buy_info = False, None
    # 60分钟卖点检测（区分一卖/二卖/三卖）
    h60_sell_signals = {"first_sell": False, "second_sell": False, "third_sell": False}
    if h60_result and h60_result.get("data"):
        h60_data = h60_result["data"]
        h60_centrals = h60_result.get("centrals", [])
        h60_pens = h60_result.get("pens_effective", [])
        h60_fractals = h60_result.get("fractals", [])
        try:
            from services.buy_sell_signals import (
                _detect_first_buy_point,
                _detect_second_buy_point,
                _detect_third_buy_point,
                _detect_first_sell_point,
                _detect_second_sell_point,
                _detect_third_sell_point,
            )
        except Exception as e:
            logging.warning("trade_command_engine: %s 60m买卖点模块导入异常: %s", code, e)
        else:
            if _detect_first_buy_point:
                try:
                    h60_first_buy, h60_first_buy_info = _detect_first_buy_point(
                        h60_data, h60_centrals, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 一买检测异常", code)
            if _detect_second_buy_point:
                try:
                    h60_second_buy, h60_second_buy_info = _detect_second_buy_point(
                        h60_data, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 二买检测异常", code)
            if _detect_third_buy_point:
                try:
                    h60_third_buy, h60_third_buy_info = _detect_third_buy_point(
                        h60_data, h60_centrals, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 三买检测异常", code)
            # 卖点检测（含 info，用于后续失效检查）
            first_sell_info = None
            second_sell_info = None
            if _detect_first_sell_point:
                try:
                    h60_sell_signals["first_sell"], first_sell_info = _detect_first_sell_point(
                        h60_data, h60_centrals, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 一卖检测异常", code)
            if _detect_second_sell_point:
                try:
                    h60_sell_signals["second_sell"], second_sell_info = _detect_second_sell_point(
                        h60_data, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 二卖检测异常", code)
            if _detect_third_sell_point:
                try:
                    h60_sell_signals["third_sell"] = _detect_third_sell_point(
                        h60_data, h60_centrals, h60_pens, h60_fractals
                    )
                except Exception:
                    logging.exception("trade_command_engine: %s 三卖检测异常", code)

            # ========== 卖点失效检查（前移至状态机之前，与前端对齐） ==========
            # 规则1：一卖触发后，若后续K线高点突破一卖最高点，则一卖结构被破坏
            if h60_sell_signals["first_sell"] and first_sell_info:
                sell1_high = first_sell_info.get("high", 0)
                sell1_date = first_sell_info.get("date", "")
                sell1_idx = -1
                for i, d in enumerate(h60_data):
                    if d.get("date") == sell1_date:
                        sell1_idx = i
                        break
                if sell1_idx >= 0:
                    for i in range(sell1_idx + 1, len(h60_data)):
                        if h60_data[i].get("high", 0) > sell1_high:
                            h60_sell_signals["first_sell"] = False
                            break

            # 规则2：二卖依赖一卖存在，一卖失效则二卖必须同步失效
            if h60_sell_signals["second_sell"] and not h60_sell_signals["first_sell"]:
                h60_sell_signals["second_sell"] = False

            # 规则3：二卖触发后，若后续K线高点突破一卖最高点，说明多头已破坏M头结构，二卖失效
            if (
                h60_sell_signals["second_sell"]
                and h60_sell_signals["first_sell"]
                and second_sell_info
            ):
                sell1_high = first_sell_info.get("high", 0) if first_sell_info else 0
                sell2_date = second_sell_info.get("date", "")
                sell2_idx = -1
                for i, d in enumerate(h60_data):
                    if d.get("date") == sell2_date:
                        sell2_idx = i
                        break
                if sell2_idx >= 0:
                    for i in range(sell2_idx + 1, len(h60_data)):
                        if h60_data[i].get("high", 0) > sell1_high:
                            h60_sell_signals["second_sell"] = False
                            break

    # 15分钟分析
    h15_bottom_div = False
    h15_top_div = False
    h15_trend_div: TrendDivergenceResult = TrendDivergenceResult()
    h15_level_alignment: LevelAlignmentResult = LevelAlignmentResult()

    if h15_result and h15_result.get("data") and h15_result.get("pens_effective"):
        h15_bottom_div = _detect_15m_bottom_divergence(
            h15_result["data"], h15_result["pens_effective"]
        )
        h15_top_div = _detect_15m_top_divergence(
            h15_result["data"], h15_result["pens_effective"]
        )
        h15_trend_div = _detect_15m_trend_bottom_divergence(
            h15_result["data"],
            h15_result.get("centrals", []),
            h15_result.get("pens_effective", []),
            h15_result.get("fractals", []),
        )
        h60_pens_eff = h60_result.get("pens_effective", []) if h60_result else []
        h15_level_alignment = _check_level_alignment_15m_to_60m(h15_trend_div, h60_pens_eff)

    is_holding = code in holding_codes

    # === 优先级 1：跌破 min(A-ZD, C-ZD)（死亡区）===
    if latest_close is not None and min_zd is not None and latest_close < min_zd - _EPS:
        state = "SELL" if is_holding else "IGNORE"
        reason = "持仓跌破战略底线，强制清仓" if is_holding else "跌破战略底线，拉黑"
    # === 优先级 3：60m 红柱缩短 + 15m 顶背驰（增加 MACD 过滤，防止卖飞）===
    elif h60_conditions["last_pen_up"] and h60_conditions.get("macd_sell") and h15_top_div:
        state = "SELL" if is_holding else "IGNORE"
        reason = "60分钟红柱缩短+15分钟顶背驰"
    # === 优先级 4：60m 一/二/三卖 -> SELL ===
    elif any(h60_sell_signals.values()):
        state = "SELL" if is_holding else "IGNORE"
        if h60_sell_signals["first_sell"]:
            reason = "60分钟一卖确认，趋势转折"
        elif h60_sell_signals["second_sell"]:
            reason = "60分钟二卖确认，反弹无力"
        elif h60_sell_signals["third_sell"]:
            reason = "60分钟三卖确认，中枢破位"
        else:
            reason = "60分钟卖点确认"
    # === 优先级 5：持仓中 + 安全向上笔 -> HOLD ===
    elif is_holding and h60_conditions["last_pen_up"]:
        state = "HOLD"
        buy_hint = _build_buy_hint_for_holding(
            h60_second_buy, h60_third_buy, h60_first_buy, h15_top_div
        )
        if buy_hint:
            reason = f"持仓中，安全向上笔，{buy_hint}"
        else:
            reason = "持仓中，安全向上笔"
    # === 优先级 5.5：持仓兜底保护 ===
    elif is_holding:
        state = "HOLD"
        buy_hint = _build_buy_hint_for_holding(
            h60_second_buy, h60_third_buy, h60_first_buy, h15_top_div
        )
        if buy_hint:
            reason = f"持仓中，{buy_hint}，无明确卖点，继续持仓"
        else:
            reason = "持仓中，无明确卖点，继续观望"
    # === 优先级 6：非持仓观望，但不禁止买点 ===
    # 注：大盘状态不再影响个股交易决策，仅防线跌破和缠论信号驱动交易
    # === 优先级 7：防线安全 + 60m买点 + 15m底背驰 -> BUY ===
    else:
        h15_micro_buy = h15_bottom_div or (
            h15_trend_div.has_signal and h15_level_alignment.is_aligned
        )

        if (
            latest_close is not None
            and latest_close >= min_zd - _EPS
            and h15_micro_buy
        ):
            # 判定 60m 买点类型（优先级：二买 > 三买 > 一买）
            if h60_second_buy:
                state = "BUY_2"
                h60_buy_type = "second_buy"
                reason = "右侧二买确认，底部确立！"
            elif h60_third_buy:
                state = "BUY_3"
                h60_buy_type = "third_buy"
                reason = "三买突破确认，顺势跟进。"
            elif h60_first_buy:
                state = "BUY_1"
                h60_buy_type = "first_buy"
                reason = "左侧一买确认，轻仓试探。"
            else:
                state = "IGNORE"
                reason = "中枢震荡，无买卖点"
        else:
            state = "IGNORE"
            reason = "中枢震荡，无买卖点"

    return {
        "state": state,
        "reason": reason,
        "daily_close": daily_close,
        "latest_close": latest_close,
        "daily_czd": daily_czd,
        "daily_azd": daily_azd,
        "h60_conditions": h60_conditions,
        "h60_buy_type": h60_buy_type,
        "h60_first_buy_info": h60_first_buy_info,
        "h60_second_buy_info": h60_second_buy_info,
        "h60_third_buy_info": h60_third_buy_info,
        "h60_sell_signals": h60_sell_signals,
        "buy_signals": {
            "first_buy": h60_first_buy,
            "second_buy": h60_second_buy,
            "third_buy": h60_third_buy,
        },
        "sell_signals": dict(h60_sell_signals),
        "first_sell_info": first_sell_info,
        "second_sell_info": second_sell_info,
        "h15_bottom_div": h15_bottom_div,
        "h15_top_div": h15_top_div,
        "h15_trend_div": h15_trend_div,
        "h15_level_alignment": h15_level_alignment,
        "is_holding": is_holding,
    }


# ---------------------------------------------------------------------------
# 三维雷达自检构建
# ---------------------------------------------------------------------------

def _build_radar_checklist(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建三维共振雷达自检结果：
    - 🌍 宏观(日线)：强势区 / 死亡区
    - ⚔️ 战役(60m)：一买/二买/三买预警 或 卖点预警
    - 🎯 微观(15m)：底背驰/顶背驰确认
    """
    daily_close = analysis.get("daily_close")
    latest_close = analysis.get("latest_close", daily_close)
    daily_czd = analysis.get("daily_czd")
    daily_azd = analysis.get("daily_azd")
    h60_conditions = analysis.get("h60_conditions", {})
    h60_buy_type = analysis.get("h60_buy_type")
    h15_bottom_div = analysis.get("h15_bottom_div", False)
    h15_top_div = analysis.get("h15_top_div", False)
    h15_trend_div = analysis.get("h15_trend_div", TrendDivergenceResult())
    h15_level_alignment = analysis.get("h15_level_alignment", LevelAlignmentResult())

    # 宏观(日线)：统一以 min(A-ZD, C-ZD) 为防线基准
    # 优先使用 latest_close（更高频价格）做风控判断，与状态机和CSV保持一致
    check_price = latest_close if latest_close is not None else daily_close
    if check_price is not None and daily_czd is not None and daily_azd is not None:
        min_zd = min(daily_azd, daily_czd)
        macro_ok = check_price >= min_zd - _EPS
        if check_price >= min_zd - _EPS:
            macro_zone = "强势区"
            macro_text = f"现价 {check_price:.2f} >= 防线 min-ZD {min_zd:.2f} (A-ZD: {daily_azd:.2f}, C-ZD: {daily_czd:.2f})"
        else:
            macro_zone = "死亡区"
            macro_text = f"现价 {check_price:.2f} < 防线 min-ZD {min_zd:.2f} (A-ZD: {daily_azd:.2f}, C-ZD: {daily_czd:.2f})"
    else:
        macro_ok = False
        macro_zone = "未知"
        macro_text = "日线数据不足"

    # 战役(60分钟)
    if h60_buy_type:
        battle_ok = True
    else:
        battle_ok = h60_conditions.get("in_c_central", False) or h60_conditions.get("last_pen_up", False)
    if h60_buy_type == "first_buy":
        battle_text = "一买预警（左侧底背驰，暴跌创新低）"
    elif h60_buy_type == "second_buy":
        battle_text = "二买预警（右侧底确认，回踩不破低点）"
    elif h60_buy_type == "third_buy":
        battle_text = "三买预警（突破回踩，悬空不破ZG）"
    elif h60_conditions.get("in_c_central"):
        battle_text = "回踩 ZD 支撑，当前处于 C 中枢内"
    elif h60_conditions.get("last_pen_up"):
        battle_text = "向上笔进行中，未触发卖点"
    elif h60_conditions.get("switched_up_to_down"):
        battle_text = "向上笔转向下笔，卖点触发"
    else:
        battle_text = "中枢震荡，无明确方向"

    # 微观(15分钟)
    if h15_trend_div.has_signal:
        micro_ok = True
        div_type = "趋势底背驰" if h15_trend_div.divergence_type == "trend" else "盘整底背驰"
        ratio_text = f"(面积比{h15_trend_div.area_ratio:.2f})"
        if h15_level_alignment.is_aligned:
            micro_text = f"底背驰已确认 — {div_type}{ratio_text} | {h15_level_alignment.reason}"
        else:
            micro_text = f"底背驰信号 — {div_type}{ratio_text} | ⚠️ {h15_level_alignment.reason}"
    elif h15_bottom_div:
        micro_ok = True
        micro_text = "底背驰已确认（传统盘整底背驰）"
    elif h15_top_div:
        micro_ok = True
        micro_text = "顶背驰已确认"
    else:
        micro_ok = False
        micro_text = "无背驰信号"

    return {
        "macro_ok": macro_ok,
        "macro_zone": macro_zone,
        "macro_text": macro_text,
        "battle_ok": battle_ok,
        "battle_text": battle_text,
        "micro_ok": micro_ok,
        "micro_text": micro_text,
    }


# ---------------------------------------------------------------------------
# 傻瓜式仓位计算器
# ---------------------------------------------------------------------------

def _calculate_order_amount(signal_type: str, current_holding: int) -> Tuple[int, str]:
    """
    傻瓜式仓位计算器。
    返回: (操作金额, 操作描述)
    """
    if signal_type == "BUY_1":
        if current_holding == 0:
            return 10_000, "买入 10,000 元"
        return 0, "已有持仓，一买不再加仓"
    elif signal_type == "BUY_2":
        amount = FIXED_TRADE_AMOUNT - current_holding
        if amount > 0:
            return amount, f"买入 {amount:,} 元"
        return 0, "已满仓，无需加仓"
    elif signal_type == "BUY_3":
        target = 25_000
        if current_holding == 0:
            return target, f"买入 {target:,} 元"
        elif current_holding < target:
            amount = target - current_holding
            return amount, f"买入 {amount:,} 元"
        return 0, "已达三买仓位上限，持仓不动"
    elif signal_type == "SELL":
        if current_holding > 0:
            return current_holding, f"卖出 {current_holding:,} 元"
        return 0, "空仓，无需卖出"
    return 0, "持兵不动"


# ---------------------------------------------------------------------------
# 军机处指令生成（极简 Markdown）
# ---------------------------------------------------------------------------

def _generate_command(
    state: str, name: str, code: str, radar: Dict[str, Any], analysis: Dict[str, Any], current_holding: int
) -> str:
    """根据状态生成包含具体买卖金额的极简操作建议。"""
    order_amount, order_desc = _calculate_order_amount(state, current_holding)

    # 止损线：优先从 60m 买点信息提取，其次用日线 C-ZD
    stop_loss: Optional[float] = None
    h60_second = analysis.get("h60_second_buy_info")
    h60_first = analysis.get("h60_first_buy_info")
    h60_third = analysis.get("h60_third_buy_info")
    if h60_second and h60_second.get("stop_loss"):
        stop_loss = float(h60_second["stop_loss"])
    elif h60_first and h60_first.get("stop_loss"):
        stop_loss = float(h60_first["stop_loss"])
    elif h60_third and h60_third.get("stop_loss"):
        stop_loss = float(h60_third["stop_loss"])
    else:
        stop_loss = analysis.get("daily_czd")

    lines: List[str] = []
    lines.append("- **【极简操作指令】**：")

    if state == "BUY_1":
        lines.append("  - 🚦 **状态**：🟢 一买轻仓试探")
        lines.append(f"  - 💰 **操作**：{order_desc}")
        if stop_loss is not None:
            lines.append(f"  - 🛡️ **防守**：绝对止损位设于 {stop_loss:.2f}。")
        else:
            lines.append("  - 🛡️ **防守**：止损位未计算，以日线 C-ZD 为参考。")
    elif state == "BUY_2":
        lines.append("  - 🚦 **状态**：🟢 二买重仓出击")
        if order_amount > 0:
            lines.append(f"  - 💰 **操作**：{order_desc}，打满 50,000 元！")
        else:
            lines.append(f"  - 💰 **操作**：{order_desc}")
        if stop_loss is not None:
            lines.append(f"  - 🛡️ **防守**：绝对止损位设于 {stop_loss:.2f}。")
        else:
            lines.append("  - 🛡️ **防守**：止损位未计算，以日线 C-ZD 为参考。")
    elif state == "BUY_3":
        lines.append("  - 🚦 **状态**：🟢 三买顺势追击")
        lines.append(f"  - 💰 **操作**：{order_desc}")
        if stop_loss is not None:
            lines.append(f"  - 🛡️ **防守**：绝对止损位设于 {stop_loss:.2f}。")
        else:
            lines.append("  - 🛡️ **防守**：止损位未计算，以日线 C-ZD 为参考。")
    elif state == "SELL":
        lines.append("  - 🚦 **状态**：🔴 强制清仓")
        lines.append(f"  - 💰 **操作**：{order_desc}")
        lines.append("  - 🛡️ **防守**：已触发风控，无防守必要。")
    elif state == "HOLD":
        lines.append("  - 🚦 **状态**：🟡 持仓观望")
        lines.append("  - 💰 **操作**：持兵不动")
        if stop_loss:
            lines.append(f"  - 🛡️ **防守**：绝对止损位设于 {stop_loss:.2f}。")
        else:
            lines.append("  - 🛡️ **防守**：空仓无防守。")
    else:
        lines.append("  - 🚦 **状态**：⚪ 空仓等待")
        lines.append("  - 💰 **操作**：持兵不动")
        lines.append("  - 🛡️ **防守**：空仓无防守。")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown 报告追加写入
# ---------------------------------------------------------------------------

def _state_label(state: str) -> str:
    labels = {
        "SELL": "🔴 空仓警报",
        "BUY_1": "🟢 一买轻仓试探",
        "BUY_2": "🟢 二买重仓出击",
        "BUY_3": "🟢 三买顺势追击",
        "HOLD": "🟡 持仓观望",
        "IGNORE": "⚪ 放弃狙击",
    }
    return labels.get(state, "⚪ 放弃狙击")


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
    追加写入 Markdown 报告（极简三维共振格式）。
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
        lines.append("- **上证指数 (000001.SH)**：数据不足")
    lines.append(f"- **大盘状态**：{_market_state_label(market_state)}")
    lines.append(f"- **风控策略**：{market_reason}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ==================== 个股作战指令 ====================
    lines.append("#### 🛡️ 【个股作战指令】")
    lines.append("")

    active_records = [r for r in records if r["state"] != "IGNORE"]
    ignore_records = [r for r in records if r["state"] == "IGNORE"]

    # 有效区域：完整输出三维共振雷达 + 极简操作指令
    for idx, rec in enumerate(active_records, start=1):
        radar = rec["radar"]
        state = rec["state"]
        pos = rec.get("current_holding", 0)
        lines.append(
            f"**{idx}. {rec['name']} ({rec['code']}) | 当前持仓：{pos:,} / {FIXED_TRADE_AMOUNT:,} 元**"
        )
        lines.append(f"- **【当前状态】**：{_state_label(state)}")
        lines.append("- **【三维共振雷达】**：")
        m_ok = "√" if radar["macro_ok"] else "×"
        b_ok = "√" if radar["battle_ok"] else "×"
        u_ok = "√" if radar["micro_ok"] else "×"
        lines.append(f"  - 🌍 {m_ok} 宏观(日线)：{radar['macro_zone']} | {radar['macro_text']}")
        lines.append(f"  - ⚔️ {b_ok} 战役(60m)：{radar['battle_text']}")
        lines.append(f"  - 🎯 {u_ok} 微观(15m)：{radar['micro_text']}")
        lines.append(rec["command"])
        lines.append("")
        lines.append("---")
        lines.append("")

    # 无效区域：极简折叠
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

def run_trade_command_engine(generate_report: bool = True) -> Optional[Path]:
    """
    主入口：拉取 -> 计算 -> 判定 -> (可选)写入报告 -> 返回文件路径。
    控制台仅打印一句：[SUCCESS] HH:mm 巡航完毕，报告已生成

    Args:
        generate_report: 为 True 时生成 Markdown 报告；为 False 时仅计算状态机并写入 CSV 快照。
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
    holding_amounts = _load_holding_amounts()
    daily_start = _daily_start_date()
    h60_start = _h60_start_date()
    h15_start = _h15_start_date()

    # ==================== 第一层：全局大盘风控 ====================
    index_daily: Optional[Dict[str, Any]] = None
    index_h60: Optional[Dict[str, Any]] = None
    index_h15: Optional[Dict[str, Any]] = None

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

    try:
        index_h15 = get_index_kline(
            symbol=INDEX_CODE,
            start_date=h15_start,
            end_date=None,
            period="15",
            refresh=False,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("trade_command_engine: 大盘15m拉取失败: %s", e)

    market_info = _compute_market_state(index_daily, index_h60, index_h15)
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

            # 复用状态机已检测的买卖点结果（避免重复检测导致的一致性问题）
            buy_signals = analysis.get(
                "buy_signals",
                {"first_buy": False, "second_buy": False, "third_buy": False},
            )
            first_buy_info = analysis.get("h60_first_buy_info")
            second_buy_info = analysis.get("h60_second_buy_info")
            third_buy_info = analysis.get("h60_third_buy_info")

            sell_signals = analysis.get(
                "sell_signals",
                {"first_sell": False, "second_sell": False, "third_sell": False},
            )
            first_sell_info = analysis.get("first_sell_info")
            second_sell_info = analysis.get("second_sell_info")

            # ========== 买点有效性检查（与前端 computeHourlyBuySellState 对齐） ==========
            # 二买必须同时满足：日线支撑 + MACD买入 + 价格高于一买低点
            if buy_signals["second_buy"] and second_buy_info:
                filter_reasons: List[str] = []
                check_price_v = analysis.get("latest_close") or analysis.get("daily_close")
                daily_azd_v = analysis.get("daily_azd")
                daily_czd_v = analysis.get("daily_czd")
                if check_price_v is not None and daily_azd_v is not None and daily_czd_v is not None:
                    if float(check_price_v) < min(float(daily_azd_v), float(daily_czd_v)):
                        filter_reasons.append("价格跌破日线支撑")
                h60_conds = analysis.get("h60_conditions", {})
                if not h60_conds.get("macd_buy"):
                    filter_reasons.append("MACD未满足买入条件")
                buy1_stop = second_buy_info.get("buy1_stop")
                stop_loss_v = second_buy_info.get("stop_loss")
                if buy1_stop is not None and stop_loss_v is not None and float(stop_loss_v) <= float(buy1_stop):
                    filter_reasons.append("二买价格未高于一买低点")
                if filter_reasons:
                    buy_signals["second_buy"] = False
                    if state != "SELL":
                        # 按优先级重新评估 state：二买 > 三买 > 一买
                        if buy_signals["third_buy"]:
                            state = "BUY_3"
                            new_reason = "三买突破确认，顺势跟进。"
                            analysis["h60_buy_type"] = "third_buy"
                        elif buy_signals["first_buy"]:
                            state = "BUY_1"
                            new_reason = "左侧一买确认，轻仓试探。"
                            analysis["h60_buy_type"] = "first_buy"
                        else:
                            if code in holding_codes:
                                state = "HOLD"
                                new_reason = "持仓中，无明确买点，继续观望"
                            else:
                                state = "IGNORE"
                                new_reason = "中枢震荡，无买卖点"
                            analysis["h60_buy_type"] = None
                        analysis["state"] = state
                        analysis["reason"] = f"{new_reason}（二买不成立：{'；'.join(filter_reasons)}）"
                    else:
                        analysis["h60_buy_type"] = None

            # 三买必须同时满足：日线支撑 + 不在C中枢内（突破中枢ZG）
            if buy_signals["third_buy"] and third_buy_info:
                filter_reasons3: List[str] = []
                check_price_v3 = analysis.get("latest_close") or analysis.get("daily_close")
                daily_azd_v3 = analysis.get("daily_azd")
                daily_czd_v3 = analysis.get("daily_czd")
                if check_price_v3 is not None and daily_azd_v3 is not None and daily_czd_v3 is not None:
                    if float(check_price_v3) < min(float(daily_azd_v3), float(daily_czd_v3)):
                        filter_reasons3.append("价格跌破日线支撑")
                h60_conds3 = analysis.get("h60_conditions", {})
                if h60_conds3.get("in_c_central"):
                    filter_reasons3.append("价格仍在C中枢内（未突破ZG）")
                if filter_reasons3:
                    buy_signals["third_buy"] = False
                    if state != "SELL":
                        # 按优先级重新评估 state：二买 > 三买 > 一买
                        if buy_signals["second_buy"]:
                            state = "BUY_2"
                            new_reason = "右侧二买确认，底部确立！"
                            analysis["h60_buy_type"] = "second_buy"
                        elif buy_signals["first_buy"]:
                            state = "BUY_1"
                            new_reason = "左侧一买确认，轻仓试探。"
                            analysis["h60_buy_type"] = "first_buy"
                        else:
                            if code in holding_codes:
                                state = "HOLD"
                                new_reason = "持仓中，无明确买点，继续观望"
                            else:
                                state = "IGNORE"
                                new_reason = "中枢震荡，无买卖点"
                            analysis["h60_buy_type"] = None
                        analysis["state"] = state
                        analysis["reason"] = f"{new_reason}（三买不成立：{'；'.join(filter_reasons3)}）"
                    else:
                        analysis["h60_buy_type"] = None

            # 一买必须同时满足：日线支撑 + 有底背驰
            if buy_signals["first_buy"] and first_buy_info:
                filter_reasons1: List[str] = []
                check_price_v1 = analysis.get("latest_close") or analysis.get("daily_close")
                daily_azd_v1 = analysis.get("daily_azd")
                daily_czd_v1 = analysis.get("daily_czd")
                if check_price_v1 is not None and daily_azd_v1 is not None and daily_czd_v1 is not None:
                    if float(check_price_v1) < min(float(daily_azd_v1), float(daily_czd_v1)):
                        filter_reasons1.append("价格跌破日线支撑")
                h60_conds1 = analysis.get("h60_conditions", {})
                if not h60_conds1.get("has_bottom_div_in_switch"):
                    filter_reasons1.append("无底背驰确认")
                if filter_reasons1:
                    buy_signals["first_buy"] = False
                    if state != "SELL":
                        # 按优先级重新评估 state：二买 > 三买 > 一买
                        if buy_signals["second_buy"]:
                            state = "BUY_2"
                            new_reason = "右侧二买确认，底部确立！"
                            analysis["h60_buy_type"] = "second_buy"
                        elif buy_signals["third_buy"]:
                            state = "BUY_3"
                            new_reason = "三买突破确认，顺势跟进。"
                            analysis["h60_buy_type"] = "third_buy"
                        else:
                            if code in holding_codes:
                                state = "HOLD"
                                new_reason = "持仓中，无明确买点，继续观望"
                            else:
                                state = "IGNORE"
                                new_reason = "中枢震荡，无买卖点"
                            analysis["h60_buy_type"] = None
                        analysis["state"] = state
                        analysis["reason"] = f"{new_reason}（一买不成立：{'；'.join(filter_reasons1)}）"
                    else:
                        analysis["h60_buy_type"] = None

            # 显式同步过滤后的买点信号回 analysis，避免隐式副作用依赖
            analysis["buy_signals"] = buy_signals

            # 写入15分钟级快照日志（无侵入式，异常不阻塞主逻辑）
            try:
                from utils.csv_logger import build_snapshot_data, log_snapshot
                snapshot = build_snapshot_data(
                    timestamp=timestamp,
                    code=code,
                    name=name,
                    market_state=market_state,
                    analysis=analysis,
                    h60_result=h60_result,
                    h15_result=h15_result,
                    sell_signals=analysis["h60_sell_signals"],
                    buy_signals=buy_signals,
                )
                log_snapshot(snapshot)
            except Exception:
                logging.warning("trade_command_engine: CSV快照写入失败 %s", code, exc_info=True)

            radar = _build_radar_checklist(analysis)
            current_holding = holding_amounts.get(code, 0)
            command = _generate_command(state, name, code, radar, analysis, current_holding)
            records.append({
                "code": code,
                "name": name,
                "state": state,
                "radar": radar,
                "command": command,
                "current_holding": current_holding,
            })
        except Exception as e:  # noqa: BLE001
            logging.warning("trade_command_engine: 标的 %s 分析失败: %s", code, e)

    # 按状态优先级排序：SELL > BUY_2 > BUY_3 > BUY_1 > HOLD > IGNORE
    priority = {"SELL": 0, "BUY_2": 1, "BUY_3": 2, "BUY_1": 3, "HOLD": 4, "IGNORE": 5}
    records.sort(key=lambda r: priority.get(r["state"], 99))

    # ==================== 第四层：Markdown 报告生成 / 仅快照模式 ====================
    if generate_report:
        path = _append_trade_report(records, timestamp, market_info)
        print(f"[SUCCESS] {time_str} 巡航完毕，报告已生成")
        return path
    else:
        logging.info("trade_command_engine: 15分钟快照模式，跳过 Markdown 报告生成（%d 条记录已写入 CSV）", len(records))
        return None
