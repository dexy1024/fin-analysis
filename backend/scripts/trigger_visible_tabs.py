#!/usr/bin/env python3
"""
手动触发标的显示脚本
用于重新显示被关闭（X掉）但满足条件的标的

用法:
    python scripts/trigger_visible_tabs.py           # 显示所有满足条件的标的
    python scripts/trigger_visible_tabs.py 600873    # 强制显示指定标的
"""

import json
import sys
from pathlib import Path


def load_summary():
    """加载雷达摘要"""
    # 从项目根目录查找
    project_root = Path(__file__).parent.parent.parent
    summary_path = project_root / "logs" / "defense_radar" / "last_summary.json"
    with open(summary_path, 'r') as f:
        return json.load(f)


def check_conditions(symbol_data: dict) -> dict:
    """检查标的满足哪些显示条件"""
    result = {
        "code": symbol_data.get("code"),
        "name": symbol_data.get("name"),
        "condition1": False,  # has_alert + pen_60m向下
        "condition2": False,  # 7个买点条件全绿
        "details": {}
    }
    
    # 条件1: has_alert = true 且 pen_60m = "向下"
    has_alert = symbol_data.get("has_alert", False)
    pen_60m = symbol_data.get("pen_60m", "")
    result["condition1"] = has_alert and pen_60m == "向下"
    result["details"]["has_alert"] = has_alert
    result["details"]["pen_60m"] = pen_60m
    
    # 条件2: 7个买点条件全部满足
    buy_conditions = [
        symbol_data.get('radar_zone_ok'),
        symbol_data.get('pen_60m_down'),
        symbol_data.get('macd_momentum_ok'),
        symbol_data.get('blue_triangle_strict'),
        symbol_data.get('in_c_central'),
        symbol_data.get('has_bottom_div_in_switch'),
        symbol_data.get('boll_buy')
    ]
    result["condition2"] = all(buy_conditions)
    result["details"]["buy_conditions_met"] = sum(1 for x in buy_conditions if x)
    result["details"]["buy_conditions_total"] = len(buy_conditions)
    
    return result


def main():
    data = load_summary()
    symbols = data.get("symbols", [])
    
    # 基础始终显示的标的
    base_always_visible = {'sh000001', '510300', '159915', '588000', '513130'}
    
    # 如果指定了标的代码，只检查该标的
    target_code = sys.argv[1] if len(sys.argv) > 1 else None
    
    print("=" * 60)
    print("手动触发标的显示检查")
    print(f"数据时间: {data.get('generated_at', '未知')}")
    if target_code:
        print(f"目标标的: {target_code}")
    print("=" * 60)
    print()
    
    triggered = []
    
    for s in symbols:
        code = s.get("code", "")
        
        # 跳过基础标的
        if code in base_always_visible:
            continue
        
        # 如果指定了目标代码，只处理该标的
        if target_code and code != target_code:
            continue
        
        result = check_conditions(s)
        
        # 只要满足任一条件就触发
        if result["condition1"] or result["condition2"]:
            triggered.append(result)
            print(f"✓ {result['name']} ({code})")
            if result["condition1"]:
                print(f"  条件1: 有预警 + 60分钟笔向下")
            if result["condition2"]:
                print(f"  条件2: 7个买点条件全绿 ({result['details']['buy_conditions_met']}/7)")
            print()
    
    print("-" * 60)
    print(f"总计可触发显示的标的: {len(triggered)} 个")
    print()
    
    if triggered:
        print("这些标的应该会在前端显示（如果之前被X关闭了，刷新页面后会重新显示）")
        print()
        print("操作提示:")
        print("1. 刷新浏览器页面")
        print("2. 或清除 localStorage 中的 'fin-analysis-closed-tabs-v1'")
    else:
        print("没有满足显示条件的标的")
    
    return triggered


if __name__ == "__main__":
    main()
