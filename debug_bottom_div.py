#!/usr/bin/env python3
"""
调试底背驰计算逻辑 - 分析恒瑞医药的数据
"""
import sys
sys.path.insert(0, '/Users/yuguoq/Desktop/CursorProject/fin-analysis/backend')

from services.indicators import get_index_kline
from datetime import datetime, timedelta

def debug_bottom_divergence(code, name):
    """详细打印底背驰计算过程"""
    print(f"\n{'='*70}")
    print(f"调试底背驰: {code} {name}")
    print(f"{'='*70}")

    # 获取15分钟数据
    h15_start = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
    h15_result = get_index_kline(
        symbol=code,
        start_date=h15_start,
        period="15",
        refresh=False,
    )

    data = h15_result.get("data", [])
    pens_effective = h15_result.get("pens_effective", [])
    fractals = h15_result.get("fractals", [])

    if len(data) < 3 or len(pens_effective) < 2:
        print("数据不足")
        return

    date_to_idx = {item["date"]: i for i, item in enumerate(data)}

    # 获取有效笔
    effective_pens = [p for p in pens_effective if p.get("direction") in ("up", "down")]

    # 打印所有有效笔（最近10笔）
    print(f"\n最近10笔序列:")
    for i, pen in enumerate(effective_pens[-10:]):
        marker = " <-- 当前笔" if i == len(effective_pens[-10:]) - 1 else ""
        print(f"  笔{i}: {pen['direction']:>4} | {pen['start_date']} ~ {pen['end_date']} | "
              f"起:{pen['start_price']:.2f} 终:{pen['end_price']:.2f}{marker}")

    current_pen = effective_pens[-1]
    current_direction = current_pen.get("direction")

    if current_direction != "down":
        print(f"\n当前笔不是向下笔({current_direction})，跳过底背驰判断")
        return

    # 获取同向笔列表
    same_dir_pens = [p for p in effective_pens if p.get("direction") == "down"]
    print(f"\n向下笔列表 (共{len(same_dir_pens)}笔):")
    for i, pen in enumerate(same_dir_pens[-5:]):
        marker = " <-- 当前笔" if i == len(same_dir_pens[-5:]) - 1 else ""
        print(f"  向下笔{i}: {pen['start_date']} ~ {pen['end_date']}{marker}")

    if len(same_dir_pens) < 2:
        print("向下笔不足2笔")
        return

    compare_pen = same_dir_pens[-2]
    print(f"\n对比笔(前一个向下笔): {compare_pen['start_date']} ~ {compare_pen['end_date']}")

    # 计算MACD面积
    def calc_macd_area(pen, is_green):
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

    # 计算最近3个向下笔的绿柱面积
    print(f"\n最近3个向下笔的绿柱面积:")
    recent_3 = same_dir_pens[-3:]
    down_areas = []
    for i, pen in enumerate(recent_3):
        area = calc_macd_area(pen, is_green=True)
        down_areas.append(area)
        marker = " <-- 当前笔" if i == 2 else ""
        compare_marker = " <-- 对比笔" if i == 1 else ""
        print(f"  向下笔{i}: 面积 = {area:.4f}{marker}{compare_marker}")

    current_area = down_areas[-1]
    compare_area = down_areas[-2] if len(down_areas) >= 2 else 0
    max_recent_area = max(down_areas)

    print(f"\n{'='*70}")
    print("底背驰判断:")
    print(f"{'='*70}")

    # 前置条件1: 必须有绿柱
    print(f"\n[前置条件1] 必须有绿柱:")
    print(f"  当前笔绿柱面积: {current_area:.4f}")
    if current_area <= 0:
        print(f"  → 无绿柱，不能形成底背驰")
        return
    print(f"  → 有绿柱，通过")

    # 前置条件2: 动能加速检查（最近3笔）
    print(f"\n[前置条件2] 动能加速检查（最近3笔）:")
    print(f"  当前笔面积: {current_area:.4f}")
    print(f"  最近3笔最大面积: {max_recent_area:.4f}")
    if current_area >= max_recent_area:
        print(f"  → 当前笔面积 ≥ 最大面积，动能加速，不能形成底背驰")
        return
    print(f"  → 当前笔面积 < 最大面积，通过")

    # 条件1: 价格创新低
    current_low = min(float(current_pen.get("start_price", 0)), float(current_pen.get("end_price", 0)))
    compare_low = min(float(compare_pen.get("start_price", 0)), float(compare_pen.get("end_price", 0)))
    print(f"\n[条件1] 价格创新低:")
    print(f"  当前笔最低价: {current_low:.2f}")
    print(f"  对比笔最低价: {compare_low:.2f}")
    print(f"  是否创新低: {current_low < compare_low}")
    if current_low >= compare_low:
        print("  → 未创新低，不能形成底背驰")
        return

    # 条件2: 动能衰竭
    print(f"\n[条件2] 动能衰竭判断:")
    print(f"  当前笔绿柱面积: {current_area:.4f}")
    print(f"  对比笔绿柱面积: {compare_area:.4f}")

    # 面积背驰判断
    area_divergence = compare_area > 0 and current_area < compare_area
    print(f"\n  面积背驰: {current_area:.4f} < {compare_area:.4f} = {area_divergence}")

    # DIF背离判断
    def get_dif_extreme(pen, find_max):
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        dif_values = [item.get("macd", {}).get("dif") for item in data[s_idx:e_idx + 1] if item.get("macd", {}).get("dif") is not None]
        if not dif_values:
            return 0.0
        return max(dif_values) if find_max else min(dif_values)

    current_dif_min = get_dif_extreme(current_pen, find_max=False)
    compare_dif_min = get_dif_extreme(compare_pen, find_max=False)
    dif_divergence = compare_area > 0 and current_dif_min > compare_dif_min

    print(f"\n  DIF背离:")
    print(f"    当前笔DIF最小值: {current_dif_min:.4f}")
    print(f"    对比笔DIF最小值: {compare_dif_min:.4f}")
    print(f"    DIF背离: {current_dif_min:.4f} > {compare_dif_min:.4f} = {dif_divergence}")

    # 条件3: 底分型
    def has_fractal_at_end(pen, fractal_type):
        pen_end_date = pen.get("end_date")
        if not pen_end_date or not fractals:
            return False
        for f in fractals:
            if f.get("type") == fractal_type and f.get("date") == pen_end_date:
                return True
        return False

    has_bottom = has_fractal_at_end(current_pen, "bottom")
    print(f"\n[条件3] 底分型确认: {has_bottom}")

    # 最终结论
    print(f"\n{'='*70}")
    final = has_bottom and (area_divergence or dif_divergence)
    print(f"最终结果: 底分型({has_bottom}) AND (面积背驰({area_divergence}) OR DIF背离({dif_divergence}))")
    print(f"信号: {'底背驰' if final else '无信号'}")
    print(f"{'='*70}")

if __name__ == "__main__":
    # 调试恒瑞医药
    debug_bottom_divergence("600276", "恒瑞医药")
