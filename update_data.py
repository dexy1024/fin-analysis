#!/usr/bin/env python3
"""
数据更新脚本 - 手动触发 60分钟数据同步
用法: cd /Users/yuguoq/Desktop/CursorProject/fin-analysis && python3 update_data.py
"""

import sys
import os

# 切换到 backend 目录
backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend')
os.chdir(backend_dir)

# 添加 backend 到路径
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import logging
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

print("=" * 50)
print(f"开始同步 60分钟数据 + 雷达摘要")
print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 50)

try:
    print("\n[1/3] 导入模块...")
    from services.kline_scheduler import run_scheduled_slot
    
    print("[2/3] 开始更新 60分钟数据（约需 2-3 分钟）...")
    run_scheduled_slot(include_daily=False)
    
    print("\n[3/3] 更新完成!")
    
    # 显示最新文件
    print("\n最新数据文件:")
    import glob
    files = glob.glob("data/kline_60_*.csv")
    for f in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
        mtime = os.path.getmtime(f)
        dt = datetime.fromtimestamp(mtime)
        print(f"  {os.path.basename(f)} - {dt.strftime('%H:%M:%S')}")
    
    # 检查特定文件
    print("\n关键数据检查:")
    for symbol in ['sh000001', '513130', '510300']:
        filepath = f"data/kline_60_{symbol}.csv"
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                lines = f.readlines()
                if len(lines) >= 2:
                    # 获取最后两行
                    last_line = lines[-1].strip()
                    print(f"  {symbol}: {last_line}")
    
    # 雷达摘要
    radar_files = glob.glob("../logs/defense_radar/defense_radar_*.md")
    if radar_files:
        latest = max(radar_files, key=os.path.getmtime)
        mtime = os.path.getmtime(latest)
        dt = datetime.fromtimestamp(mtime)
        print(f"\n雷达摘要: {os.path.basename(latest)} ({dt.strftime('%H:%M:%S')})")
        
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 50)
print("✅ 执行完成")
print("=" * 50)
