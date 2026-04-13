#!/usr/bin/env python3
"""更新雷达数据并检查特定股票"""
import sys
import traceback

try:
    from services.defense_radar import build_defense_radar_summary

    print('开始更新双防线雷达数据...', flush=True)
    result = build_defense_radar_summary()
    print(f'更新完成，共 {len(result)} 个标的', flush=True)

    # 检查梅花生物的数据
    for item in result:
        if item['code'] == '600873':
            print('\n=== 梅花生物 600873 ===', flush=True)
            print(f'名称: {item.get("name")}', flush=True)
            print(f'现价: {item.get("last_price")}', flush=True)
            print(f'警报: {item.get("alert")}', flush=True)
            print(f'60分钟笔: {item.get("pen_60m")}', flush=True)
            conditions = [
                ('radar_zone_ok', item.get('radar_zone_ok')),
                ('pen_60m_down', item.get('pen_60m_down')),
                ('macd_momentum_ok', item.get('macd_momentum_ok')),
                ('blue_triangle_strict', item.get('blue_triangle_strict')),
                ('in_c_central', item.get('in_c_central')),
                ('has_bottom_div_in_switch', item.get('has_bottom_div_in_switch')),
                ('boll_buy', item.get('boll_buy')),
            ]
            print('\n7个买点条件:', flush=True)
            for name, val in conditions:
                status = '✓' if val else '✗'
                print(f'  {status} {name}: {val}', flush=True)
            met = sum(1 for _, v in conditions if v)
            print(f'\n满足条件数: {met}/7', flush=True)
            has_alert = item.get('has_alert')
            pen_down = item.get('pen_60m') == '向下'
            print(f'条件1 (has_alert+向下): {has_alert and pen_down}', flush=True)
            print(f'条件2 (5个+): {met >= 5}', flush=True)
            break

    print('\n数据已保存到 logs/defense_radar/last_summary.json', flush=True)
except Exception as e:
    print(f"ERROR: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)
