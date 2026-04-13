#!/bin/bash
# 数据更新脚本 - 手动触发 60分钟数据同步

cd "$(dirname "$0")/backend" || exit 1

echo "========================================"
echo "开始同步 60分钟数据 + 雷达摘要"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

/usr/bin/python3 << 'EOF'
from services.kline_scheduler import run_scheduled_slot
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

try:
    print("\n[1/2] 开始更新 60分钟数据...")
    run_scheduled_slot(include_daily=False)
    print("\n[2/2] 更新完成!")
    print("\n最新数据文件:")
    import os
    import glob
    files = glob.glob("../backend/data/kline_60_*.csv")
    for f in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
        mtime = os.path.getmtime(f)
        from datetime import datetime
        dt = datetime.fromtimestamp(mtime)
        print(f"  {os.path.basename(f)} - 更新时间: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 显示雷达摘要
    import glob
    radar_files = glob.glob("../logs/defense_radar/defense_radar_*.md")
    if radar_files:
        latest = max(radar_files, key=os.path.getmtime)
        print(f"\n雷达摘要: {os.path.basename(latest)}")
        
except Exception as e:
    print(f"\n错误: {e}")
    import traceback
    traceback.print_exc()
EOF

echo ""
echo "========================================"
echo "执行完成"
echo "========================================"
