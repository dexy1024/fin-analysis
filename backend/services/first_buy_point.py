"""
60分钟第一类买点（一买）算法识别模块

【核心定义】趋势底背驰：
- 至少两个同向向下的中枢（a+A+b+B+c 结构）
- 当前向下笔（c段）创新低（跌破B中枢低点）
- c段 MACD 绿柱面积 < b段 MACD 绿柱面积（背驰）
- 向下笔走完出现底分型

【输出】一买信号标记，包含：
- 信号类型：FIRST_BUY_POINT
- 触发日期
- 价格位置
- 背驰强度（面积比）
- 止损线（底分型最低价）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from services.indicators import get_index_kline
from services.position_manager import buy as pm_buy, get_holdings


@dataclass
class FirstBuyPointSignal:
    """一买信号数据结构"""
    code: str
    name: str
    date: str  # 触发日期（底分型日期）
    price: float  # 触发价格
    stop_loss: float  # 止损线（底分型最低价）
    area_ratio: float  # 背驰强度（c段面积 / b段面积）
    b_area: float  # b段绿柱面积
    c_area: float  # c段绿柱面积
    hub_b_low: float  # B中枢最低点
    current_low: float  # 当前向下笔最低点


def calculate_macd_green_area(
    data: List[dict],
    start_date: str,
    end_date: str,
) -> float:
    """
    计算指定区间内的 MACD 绿柱总面积
    
    Args:
        data: K线数据列表
        start_date: 开始日期
        end_date: 结束日期
    
    Returns:
        绿柱面积总和（MACD < 0 的部分）
    """
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


def find_downward_hubs(
    centrals: List[dict],
    pens_effective: List[dict],
) -> List[dict]:
    """
    识别向下的中枢（A、B中枢）

    向下中枢定义：
    - 进入段为向下笔（进入中枢的第一笔方向向下）
    - 中枢由连续的三笔重叠构成（segment_indices 有且仅有3个索引）
    - 中枢内部笔方向不完全相同（避免出现三笔同向的异常中枢）

    ⚠️ 重要：_build_centrals 使用的是 pens_effective，所以 segment_indices
    对应的是 pens_effective 的索引，而非原始 pens。

    Args:
        centrals: 中枢列表
        pens_effective: 有效笔列表（segment_indices 的索引目标）

    Returns:
        向下中枢列表（按时间排序）
    """
    downward_hubs = []

    for central in centrals:
        idxs = central.get("segment_indices", [])
        # 中枢必须由连续的三笔重叠构成
        if len(idxs) != 3:
            continue

        p1 = pens_effective[idxs[0]] if idxs[0] < len(pens_effective) else None
        p2 = pens_effective[idxs[1]] if idxs[1] < len(pens_effective) else None
        p3 = pens_effective[idxs[2]] if idxs[2] < len(pens_effective) else None
        if p1 is None or p2 is None or p3 is None:
            continue

        # 条件1：进入段（第一笔）为向下笔
        if p1.get("direction") != "down":
            continue

        # 条件2：三笔方向不完全相同（中枢内部必须有不同方向的笔重叠）
        dirs = [p1.get("direction"), p2.get("direction"), p3.get("direction")]
        if len(set(dirs)) == 1:
            continue

        downward_hubs.append({
            "start_date": central.get("start_date"),
            "end_date": central.get("end_date"),
            "zg": central.get("zg"),  # 中枢高点
            "zd": central.get("zd"),  # 中枢低点
            "direction": "down",
        })

    # 按时间排序
    downward_hubs.sort(key=lambda x: x["start_date"])
    return downward_hubs


def check_macd_zero_axis_retrace(
    data: List[dict],
    start_date: str,
    end_date: str,
) -> bool:
    """
    检查指定区间内MACD是否回抽零轴

    标准趋势背驰要求：B中枢构建期间，MACD的DIF线或MACD柱状图
    必须至少有一次上穿或触碰0轴。

    判定条件（满足其一即可）：
    - MACD柱状图 >= 0（触碰或在零轴上方）
    - DIF线 >= 0（触碰或在零轴上方）
    - MACD柱状图从负变正（上穿0轴）

    Args:
        data: K线数据列表
        start_date: 区间开始日期
        end_date: 区间结束日期

    Returns:
        是否回抽零轴
    """
    in_range = False
    prev_macd = None

    for item in data:
        date = item.get("date", "")

        if date == start_date:
            in_range = True

        if in_range:
            macd = item.get("macd", {})
            m = macd.get("macd", 0)
            dif = macd.get("dif", 0)

            # 触碰或在零轴上方
            if m is not None and m >= 0:
                return True
            if dif is not None and dif >= 0:
                return True

            # 上穿0轴：前一值为负，当前值>=0
            if prev_macd is not None and prev_macd < 0 and m is not None and m >= 0:
                return True

            prev_macd = m

        if date == end_date:
            break

    return False


def find_down_pens(pens: List[dict]) -> List[dict]:
    """
    获取所有向下笔（按时间排序）
    
    Args:
        pens: 笔列表
    
    Returns:
        向下笔列表
    """
    down_pens = [
        p for p in pens
        if p.get("direction") == "down"
    ]
    down_pens.sort(key=lambda x: x.get("start_date", ""))
    return down_pens


def _process_kline_inclusion(
    klines: List[dict],
    direction: str = "down",
) -> List[dict]:
    """
    缠论K线包含处理

    在向下趋势中（找底分型），相邻K线存在包含关系时：
    - 取高点中的较小者，低点中的较小者

    在向上趋势中（找顶分型），相邻K线存在包含关系时：
    - 取高点中的较大者，低点中的较大者

    Args:
        klines: K线列表，每个元素包含 high、low、date
        direction: "down" 表示向下趋势（找底分型），"up" 表示向上趋势

    Returns:
        处理后的K线列表
    """
    if not klines or len(klines) < 2:
        return klines

    result = [dict(klines[0])]

    for i in range(1, len(klines)):
        prev = result[-1]
        curr = klines[i]

        # 检查是否存在包含关系
        # 情况1: 当前K线包含前一根
        curr_contains_prev = (
            curr.get("high", 0) >= prev.get("high", 0)
            and curr.get("low", 0) <= prev.get("low", 0)
        )
        # 情况2: 前一根包含当前K线
        prev_contains_curr = (
            prev.get("high", 0) >= curr.get("high", 0)
            and prev.get("low", 0) <= curr.get("low", 0)
        )

        if curr_contains_prev or prev_contains_curr:
            # 存在包含关系，按趋势方向合并
            if direction == "down":
                # 向下趋势：取高点中的较小者，低点中的较小者
                merged_high = min(prev.get("high", 0), curr.get("high", 0))
                merged_low = min(prev.get("low", 0), curr.get("low", 0))
            else:
                # 向上趋势：取高点中的较大者，低点中的较大者
                merged_high = max(prev.get("high", 0), curr.get("high", 0))
                merged_low = max(prev.get("low", 0), curr.get("low", 0))

            # 用合并后的K线替换最后一根
            result[-1] = {
                "high": merged_high,
                "low": merged_low,
                "date": prev.get("date", curr.get("date")),
            }
        else:
            result.append(dict(curr))

    return result


def has_bottom_fractal(
    data: List[dict],
    date: str,
) -> bool:
    """
    检查指定日期是否有底分型（基于缠论K线包含处理后的标准化K线）

    处理流程：
    1. 提取目标日期前后各5根K线
    2. 执行向下趋势K线包含处理（向下包含原则）
    3. 在标准化K线上判断底分型

    底分型定义：中间K线的低点比左右两侧标准化K线都低

    Args:
        data: K线数据
        date: 检查日期

    Returns:
        是否有底分型
    """
    # 构建日期到索引的映射
    date_to_idx = {item.get("date"): i for i, item in enumerate(data)}
    idx = date_to_idx.get(date)

    if idx is None:
        return False

    # 提取目标日期前后各5根K线（共11根）进行包含处理
    start = max(0, idx - 5)
    end = min(len(data), idx + 6)
    klines = [
        {"high": d.get("high", 0), "low": d.get("low", 0), "date": d.get("date")}
        for d in data[start:end]
    ]

    # 向下趋势执行K线包含处理
    processed = _process_kline_inclusion(klines, direction="down")

    # 在标准化K线序列中找到目标日期对应的索引
    target_idx = None
    for i, k in enumerate(processed):
        if k.get("date") == date:
            target_idx = i
            break

    if target_idx is None or target_idx < 1 or target_idx >= len(processed) - 1:
        return False

    left = processed[target_idx - 1]
    mid = processed[target_idx]
    right = processed[target_idx + 1]

    # 底分型：中间标准化K线的低点比左右两侧都低
    return mid.get("low", 0) < left.get("low", 0) and mid.get("low", 0) < right.get("low", 0)


def detect_first_buy_point(
    code: str,
    name: str = "",
    refresh: bool = False,
) -> Optional[FirstBuyPointSignal]:
    """
    检测60分钟第一类买点（一买）
    
    【算法逻辑】
    1. 趋势确立：至少2个向下中枢（进入段向下 + 内部三笔重叠）
    2. 创新低：当前向下笔（c段）跌破B中枢最低点
    3. 回抽零轴：B中枢构建期间MACD回抽零轴确认
    4. 背驰判定：c段绿柱面积 < b段绿柱面积
    5. 止跌确认：向下笔终点出现底分型（基于标准化K线）
    
    Args:
        code: 股票代码
        name: 股票名称
        refresh: 是否强制刷新数据
    
    Returns:
        一买信号或None
    """
    try:
        # 获取60分钟数据
        result = get_index_kline(
            symbol=code,
            start_date="2026-01-01",  # 获取足够历史数据
            period="60",
            refresh=refresh,
        )
        
        data = result.get("data", [])
        centrals = result.get("centrals", [])
        # ⚠️ segment_indices 对应的是 pens_effective，必须使用 pens_effective
        pens_effective = result.get("pens_effective", [])

        if not data or not centrals or not pens_effective:
            logging.warning(f"[{code}] 数据不足，无法检测一买")
            return None

        # 1. 识别向下中枢（至少2个）
        downward_hubs = find_downward_hubs(centrals, pens_effective)
        if len(downward_hubs) < 2:
            logging.debug(f"[{code}] 向下中枢数量不足: {len(downward_hubs)}")
            return None

        # 取最后两个向下中枢作为A、B中枢
        hub_a = downward_hubs[-2]  # A中枢
        hub_b = downward_hubs[-1]  # B中枢

        # 2. 获取向下笔（基于 pens_effective）
        down_pens = find_down_pens(pens_effective)
        if len(down_pens) < 2:
            logging.debug(f"[{code}] 向下笔数量不足")
            return None
        
        # 找到B中枢后的向下笔（c段）
        # 条件：笔的结束时间 > B中枢结束时间，且笔的低点 < B中枢低点（创新低）
        hub_b_end = hub_b["end_date"]
        hub_b_low = hub_b["zd"]
        c_pen = None
        for pen in down_pens:
            pen_end = pen.get("end_date")
            pen_low = min(pen.get("start_price", 0), pen.get("end_price", 0))
            # 修改条件：笔结束时间在B中枢之后，且创新低
            if pen_end > hub_b_end and pen_low < hub_b_low:
                c_pen = pen
                break
        
        if not c_pen:
            logging.debug(f"[{code}] 未找到B中枢后的向下笔（c段）")
            return None
        
        # 找到c段之前的向下笔（b段）
        hub_a_end = hub_a["end_date"]
        b_pen = None
        for pen in down_pens:
            if pen.get("end_date") > hub_a_end and pen.get("end_date") < c_pen.get("start_date"):
                b_pen = pen
        
        if not b_pen:
            logging.debug(f"[{code}] 未找到A-B中枢间的向下笔（b段）")
            return None
        
        # 3. 检查创新低：c段低点 < B中枢低点
        c_low = min(c_pen.get("start_price", 0), c_pen.get("end_price", 0))
        hub_b_low = hub_b["zd"]

        if c_low >= hub_b_low:
            logging.debug(f"[{code}] 未创新低: c_low={c_low}, hub_b_low={hub_b_low}")
            return None

        # 4. B中枢构建期间MACD回抽零轴确认
        if not check_macd_zero_axis_retrace(
            data,
            hub_b["start_date"],
            hub_b["end_date"],
        ):
            logging.debug(f"[{code}] B中枢构建期间MACD未回抽零轴")
            return None

        # 5. 计算MACD绿柱面积
        b_area = calculate_macd_green_area(
            data,
            b_pen.get("start_date"),
            b_pen.get("end_date"),
        )
        c_area = calculate_macd_green_area(
            data,
            c_pen.get("start_date"),
            c_pen.get("end_date"),
        )
        
        if b_area <= 0 or c_area <= 0:
            logging.debug(f"[{code}] MACD面积计算异常: b_area={b_area}, c_area={c_area}")
            return None
        
        # 背驰判定：c段面积 < b段面积
        if c_area >= b_area:
            logging.debug(f"[{code}] 无背驰: c_area={c_area}, b_area={b_area}")
            return None
        
        # 5. 检查底分型
        c_end_date = c_pen.get("end_date")
        if not has_bottom_fractal(data, c_end_date):
            logging.debug(f"[{code}] 无底分型: date={c_end_date}")
            return None
        
        # 找到底分型最低价作为止损线
        stop_loss = c_low
        for item in data:
            if item.get("date") == c_end_date:
                stop_loss = item.get("low", c_low)
                break
        
        # 生成一买信号
        signal = FirstBuyPointSignal(
            code=code,
            name=name or code,
            date=c_end_date,
            price=c_pen.get("end_price", 0),
            stop_loss=stop_loss,
            area_ratio=c_area / b_area,
            b_area=b_area,
            c_area=c_area,
            hub_b_low=hub_b_low,
            current_low=c_low,
        )
        
        logging.info(
            f"[{code}] 检测到一买信号: date={signal.date}, "
            f"price={signal.price}, area_ratio={signal.area_ratio:.2f}"
        )

        # 自动记录买入持仓（一买买入 10000 元）
        try:
            # 检查是否已有该代码持仓，避免重复买入
            existing = [p for p in get_holdings() if p.code == code]
            if not existing:
                pm_buy(
                    code=code,
                    name=name or code,
                    signal_type="first_buy",
                    price=float(signal.price),
                    amount=10000.0,
                    tactical_stop=float(signal.stop_loss),
                    strategic_stop=float(signal.stop_loss),
                )
                logging.info(f"[{code}] 一买自动买入: 金额=10000元 @ {signal.price}")
            else:
                logging.info(f"[{code}] 已有持仓，跳过一买自动买入")
        except Exception as e:
            logging.warning(f"[{code}] 一买自动买入记录失败: {e}")

        return signal
        
    except Exception as e:
        logging.exception(f"[{code}] 一买检测异常: {e}")
        return None


def scan_first_buy_points(
    watchlist: List[Tuple[str, str]],
    refresh: bool = False,
) -> List[FirstBuyPointSignal]:
    """
    扫描监控列表中的一买信号
    
    Args:
        watchlist: (代码, 名称) 列表
        refresh: 是否强制刷新数据
    
    Returns:
        一买信号列表
    """
    signals = []
    
    for code, name in watchlist:
        signal = detect_first_buy_point(code, name, refresh)
        if signal:
            signals.append(signal)
    
    return signals


if __name__ == "__main__":
    # 测试
    from services.defense_radar import DEFENSE_RADAR_WATCHLIST
    
    logging.basicConfig(level=logging.INFO)
    
    print("=== 扫描一买信号 ===\n")
    
    # 测试几个标的
    test_codes = [
        ("000429", "粤高速"),
        ("000423", "东阿阿胶"),
        ("600873", "梅花生物"),
    ]
    
    for code, name in test_codes:
        print(f"\n检测 {name}({code})...")
        signal = detect_first_buy_point(code, name, refresh=False)
        if signal:
            print(f"  ✓ 一买信号!")
            print(f"    日期: {signal.date}")
            print(f"    价格: {signal.price}")
            print(f"    止损: {signal.stop_loss}")
            print(f"    背驰强度: {signal.area_ratio:.2%}")
        else:
            print(f"  ✗ 无一买信号")
