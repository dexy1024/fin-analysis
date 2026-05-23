#!/usr/bin/env python3
"""
将 backend/data/observation_shenwan_v2.json 申万二级行业跑出与
logs/snapshots_YYYY_new.csv 同字段的快照，写入 logs/snapshots_shenwan_v2_YYYY.csv。

用法（仓库根目录）：
    ./generate_snapshots_shenwan_v2.sh --write

或：
    cd backend && FIN_SNAPSHOT_ALLOW=1 python3 scripts/export_snapshots_shenwan_v2.py

说明：行业指数仅 AKShare 日线可用，无 60m/15m 本地缓存；对应列多为「无信号」/「-」。
"""

from __future__ import annotations

import logging
import os
import sys

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)

from services.trade_command_engine import export_shenwan_v2_snapshots_to_csv  # noqa: E402


def main() -> int:
    os.environ.setdefault("FIN_SNAPSHOT_ALLOW", "1")
    path = export_shenwan_v2_snapshots_to_csv()
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
