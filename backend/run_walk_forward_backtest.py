#!/usr/bin/env python3
"""
步进式回测（15m）：复用实盘信号管线。

用法：
    cd backend && python run_walk_forward_backtest.py [--start 2023-01-01] [--symbol 510300] ...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.walk_forward_backtest import main

if __name__ == "__main__":
    main()
