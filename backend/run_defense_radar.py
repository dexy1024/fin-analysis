#!/usr/bin/env python3
"""
在项目根: python backend/run_defense_radar.py
在 backend 目录: python run_defense_radar.py

默认只读本地缓存（假定 `kline_scheduler` 等前置任务已把日线/60m 写入 data/）。
日线算 C-ZD/A-ZD，60m 末根算现价。一般不加参数。
排障临时拉网再算：加 --refresh
"""

import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.defense_radar import run_defense_radar  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    refresh = "--refresh" in sys.argv
    path = run_defense_radar(refresh=refresh)
    print(path)


if __name__ == "__main__":
    main()
