#!/usr/bin/env python3
"""
手动触发作战指令引擎

用法（写盘须显式放行）：
    项目根：./generate_snapshots.sh --write [--report]
    或：cd backend && FIN_SNAPSHOT_ALLOW=1 python run_trade_command.py [--report]

效果：
    追加 logs/snapshots_YYYY.csv；加 --report 时另生成 trade_reports 作战指令 Markdown。
"""

import sys
from datetime import datetime
from pathlib import Path

# 将 backend 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.trade_command_engine import run_trade_command_engine
from utils.csv_logger import _get_csv_path

if __name__ == "__main__":
    want_report = "--report" in sys.argv
    path = run_trade_command_engine(generate_report=want_report)
    if want_report and path:
        print(f"报告路径: {path}")
    else:
        y = datetime.now().strftime("%Y")
        print(f"快照已追加: {_get_csv_path()}（年份文件 snapshots_{y}.csv）")
