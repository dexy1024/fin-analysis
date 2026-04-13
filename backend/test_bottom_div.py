#!/usr/bin/env python3
"""测试底背驰计算逻辑"""
import sys
import traceback

try:
    from services.defense_radar import (
        _macd_neg_area,
        _compute_bottom_divergence_arrows,
        _compute_hourly_buy_conditions,
    )
    from services.indicators import get_index_kline
    import json
    from datetime import datetime, timedelta

    print("=== 正在获取 600873 梅花生物 的60分钟数据... ===", flush=True)

    # 计算日期范围（180天）
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

    print(f"日期范围: {start_date} ~ {end_date}", flush=True)

    # 获取60分钟数据
    h60 = get_index_kline("600873", start_date=start_date, end_date=end_date, period="60")

    print(f"获取到的字段: {list(h60.keys())}", flush=True)

    bars = h60.get("data", [])
    pens_eff = h60.get("pens_effective", [])

    print(f"K线数量: {len(bars)}", flush=True)
    print(f"有效笔数量: {len(pens_eff)}", flush=True)

    # 只保留向下笔
    down_pens = [p for p in pens_eff if p.get("direction") == "down"]
    print(f"向下笔数量: {len(down_pens)}", flush=True)

    if len(down_pens) >= 2:
        print("\n最近两根向下笔:", flush=True)
        for i, p in enumerate(down_pens[-2:]):
            print(f"  向下笔 {i}: {p.get('start_date')} ~ {p.get('end_date')}", flush=True)
            print(f"    start_price={p.get('start_price')}, end_price={p.get('end_price')}", flush=True)

    # 计算底背驰箭头
    div_arrows = _compute_bottom_divergence_arrows(bars, pens_eff)
    print(f"\n计算出的底背驰箭头数量: {len(div_arrows)}", flush=True)
    for i, (date, y) in enumerate(div_arrows):
        print(f"  底背驰 {i}: date={date}, y={y}", flush=True)

    # 计算买点条件
    if bars:
        last_price = bars[-1].get("close", 0)
        print(f"\n最新价格: {last_price}", flush=True)

        in_c_central, has_bottom_div_in_switch, boll_buy = _compute_hourly_buy_conditions(
            h60, bars, last_price
        )
        print(f"\n买点条件计算结果:", flush=True)
        print(f"  in_c_central: {in_c_central}", flush=True)
        print(f"  has_bottom_div_in_switch: {has_bottom_div_in_switch}", flush=True)
        print(f"  boll_buy: {boll_buy}", flush=True)

    print("\n=== 测试完成! ===", flush=True)
except Exception as e:
    print(f"ERROR: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)
