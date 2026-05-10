#!/usr/bin/env python3
"""
将 backend/data/watchlist_hs300.json 全量跑出与 logs/snapshots_YYYY.csv
同字段的快照行，写入 logs/snapshots_hs300_YYYY.csv。

用法（仓库根目录）：
    PYTHONPATH=backend python3 backend/scripts/export_snapshots_hs300.py

或：
    cd backend && python3 scripts/export_snapshots_hs300.py

（不向 Web 前端注册；仅在本地产出 CSV）
"""

from __future__ import annotations

import logging
import os
import sys

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

from services.trade_command_engine import export_hs300_snapshots_to_csv  # noqa: E402


def main() -> int:
    path = export_hs300_snapshots_to_csv()
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
