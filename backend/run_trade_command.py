#!/usr/bin/env python3
"""
手动触发作战指令引擎

用法：
    cd backend && python run_trade_command.py

效果：
    静默拉取监控池标的三周期数据，在内存中完成缠论计算与状态机判定，
    生成 Markdown 作战指令报告并追加写入 trade_reports/ 目录。
"""

import sys
from pathlib import Path

# 将 backend 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.trade_command_engine import run_trade_command_engine

if __name__ == "__main__":
    path = run_trade_command_engine()
    print(f"报告路径: {path}")
