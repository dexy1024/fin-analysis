#!/usr/bin/env python3
"""
调试15分信号计算逻辑 - 分析福耀玻璃的数据
"""
import sys
sys.path.insert(0, '/Users/yuguoq/Desktop/CursorProject/fin-analysis/backend')

from services.indicators import get_index_kline
from datetime import datetime, timedelta

def debug_h15_signal(code, name):
    """详细打印15分信号计算过程"""
    print(f"\n{'='*60}")
    print(f"调试标的: {code} {name}")
    print(f"{'='*60}")

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

    print(f"\n数据概览:")
    print(f"  K线数量: {len(data)}")
    print(f"  有效笔数量: {len(pens_effective)}")
    print(f"  分型数量: {len(fractals)}")

    if len(data) < 3 or len(pens_effective) < 2:
        print("数据不足，退出")
        return

    # 打印最近5根K线
    print(f"\n最近5根15分钟K线:")
    for item in data[-5:]:
        macd = item.get("macd", {})
        print(f"  {item['date']}: 开{item['open']:.2f} 高{item['high']:.2f} 低{item['low']:.2f} 收{item['close']:.2f} | MACD:{macd.get('macd', 0):.4f} DIF:{macd.get('dif', 0):.4f}")

    # 打印所有有效笔
    print(f"\n有效笔列表 (最近5笔):")
    for i, pen in enumerate(pens_effective[-5:]):
        marker = " <-- 当前笔" if i == len(pens_effective[-5:]) - 1 else ""
        print(f"  笔{i}: {pen['direction']} | {pen['start_date']} ~ {pen['end_date']} | 起:{pen['start_price']:.2f} 终:{pen['end_price']:.2f}{marker}")

    # 构建日期到索引的映射
    date_to_idx = {item["date"]: i for i, item in enumerate(data)}

    # 获取当前笔和同向对比笔
    effective_pens = [p for p in pens_effective if p.get("direction") in ("up", "down")]
    current_pen = effective_pens[-1]
    current_direction = current_pen.get("direction")

    same_dir_pens = [p for p in effective_pens if p.get("direction") == current_direction]
    if len(same_dir_pens) < 2:
        print("\n同向笔不足2笔，无法判断背驰")
        return
    compare_pen = same_dir_pens[-2]

    print(f"\n当前笔: {current_direction} | 起:{current_pen['start_date']} 终:{current_pen['end_date']}")
    print(f"对比笔: {compare_pen['direction']} | 起:{compare_pen['start_date']} 终:{compare_pen['end_date']}")

    # 计算MACD面积
    def calc_macd_area_detail(pen, is_green):
        """详细计算MACD面积，打印过程"""
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0, []

        area = 0.0
        details = []
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None:
                if is_green and m < 0:
                    area += abs(m)
                    details.append(f"{item['date']}: MACD={m:.4f} | 累加{abs(m):.4f}")
                elif not is_green and m > 0:
                    area += abs(m)
                    details.append(f"{item['date']}: MACD={m:.4f} | 累加{abs(m):.4f}")
        return area, details

    # 计算DIF极值
    def get_dif_extreme_detail(pen, find_max):
        """详细计算DIF极值"""
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0, []

        dif_values = []
        details = []
        for item in data[s_idx:e_idx + 1]:
            dif = item.get("macd", {}).get("dif")
            if dif is not None:
                dif_values.append(dif)
                details.append(f"{item['date']}: DIF={dif:.4f}")
        if not dif_values:
            return 0.0, []
        return (max(dif_values), details) if find_max else (min(dif_values), details)

    # 检查分型
    def check_fractal(pen, fractal_type):
        pen_end_date = pen.get("end_date")
        if not pen_end_date or not fractals:
            return False
        for f in fractals:
            if f.get("type") == fractal_type and f.get("date") == pen_end_date:
                return True
        return False

    if current_direction == "down":
        print(f"\n{'='*60}")
        print("底背驰判断过程:")
        print(f"{'='*60}")

        # 条件1: 价格创新低
        current_low = min(float(current_pen.get("start_price", 0)), float(current_pen.get("end_price", 0)))
        compare_low = min(float(compare_pen.get("start_price", 0)), float(compare_pen.get("end_price", 0)))
        print(f"\n[条件1] 价格创新低:")
        print(f"  当前笔最低价: {current_low:.2f}")
        print(f"  对比笔最低价: {compare_low:.2f}")
        print(f"  是否创新低: {current_low < compare_low}")

        if current_low < compare_low:
            # 条件2a: 面积背驰
            print(f"\n[条件2a] MACD绿柱面积背驰:")
            current_area, curr_details = calc_macd_area_detail(current_pen, is_green=True)
            compare_area, comp_details = calc_macd_area_detail(compare_pen, is_green=True)

            print(f"\n  当前笔绿柱面积计算过程:")
            for d in curr_details[-5:]:  # 只打印最后5个
                print(f"    {d}")
            print(f"  当前笔绿柱总面积: {current_area:.4f}")

            print(f"\n  对比笔绿柱面积计算过程:")
            for d in comp_details[-5:]:  # 只打印最后5个
                print(f"    {d}")
            print(f"  对比笔绿柱总面积: {compare_area:.4f}")

            area_divergence = current_area > 0 and compare_area > 0 and current_area < compare_area
            print(f"\n  面积背驰判断: {current_area:.4f} < {compare_area:.4f} = {area_divergence}")

            # 条件2b: DIF背离
            print(f"\n[条件2b] DIF黄白线背离:")
            current_dif_min, curr_dif_details = get_dif_extreme_detail(current_pen, find_max=False)
            compare_dif_min, comp_dif_details = get_dif_extreme_detail(compare_pen, find_max=False)

            print(f"  当前笔DIF最小值: {current_dif_min:.4f}")
            print(f"  对比笔DIF最小值: {compare_dif_min:.4f}")
            dif_divergence = current_dif_min > compare_dif_min
            print(f"  DIF背离判断: {current_dif_min:.4f} > {compare_dif_min:.4f} = {dif_divergence}")

            # 条件3: 底分型
            print(f"\n[条件3] 底分型确认:")
            has_bottom = check_fractal(current_pen, "bottom")
            print(f"  当前笔末端({current_pen['end_date']})是否有底分型: {has_bottom}")
            if fractals:
                print(f"  所有分型: {[{'type': f['type'], 'date': f['date']} for f in fractals[-3:]]}")

            # 最终判断
            print(f"\n{'='*60}")
            final_result = has_bottom and (area_divergence or dif_divergence)
            print(f"最终判断: 底分型({has_bottom}) AND (面积背驰({area_divergence}) OR DIF背离({dif_divergence}))")
            print(f"结果: {'底背驰' if final_result else '无信号'}")
            print(f"{'='*60}")

if __name__ == "__main__":
    # 调试福耀玻璃
    debug_h15_signal("600660", "福耀玻璃")
