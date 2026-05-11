#!/usr/bin/env python3
"""
手动触发作战指令引擎

用法（写盘须带 --write / -w；仅靠环境变量 FIN_SNAPSHOT_ALLOW 不足以从本入口写盘）：
    项目根：./generate_snapshots.sh --write [--report]
    或：cd backend && python run_trade_command.py --write [--report]

效果：
    默认追加 logs/snapshots_YYYY.csv；若已 export FIN_SNAPSHOT_CSV_SUFFIX=_new 则为 snapshots_YYYY_new.csv。
    加 --report 时另生成 trade_reports 作战指令 Markdown。
"""

import os
import sys
from pathlib import Path

# 将 backend 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.trade_command_engine import run_trade_command_engine
from utils.csv_logger import _get_csv_path

if __name__ == "__main__":
    if "--write" not in sys.argv and "-w" not in sys.argv:
        raise SystemExit(
            "已拒绝写入：run_trade_command.py 必须在命令行加 --write 或 -w。\n"
            "推荐：./generate_snapshots.sh --write [--report]\n"
            "或：cd backend && python run_trade_command.py --write [--report]\n"
            "（仅 shell 里 export FIN_SNAPSHOT_ALLOW=1 而裸跑本脚本不会再写盘。）"
        )
    os.environ["FIN_SNAPSHOT_ALLOW"] = "1"
    want_report = "--report" in sys.argv
    path = run_trade_command_engine(generate_report=want_report)
    if want_report and path:
        print(f"报告路径: {path}")
    else:
        p = _get_csv_path()
        print(f"快照已追加: {p}（相对项目根 logs/）")
