#!/usr/bin/env python3
"""
基于 shenwan_v2_sector_codes.json 跑出与 logs/snapshots_YYYY_new.csv 同字段的快照，
写入 logs/snapshots_shenwan_v2_YYYY.csv（缺 60m/15m 时不写入）。

用法（仓库根目录）：
    ./generate_snapshots_shenwan_v2.sh --write

或：
    cd backend && FIN_SNAPSHOT_ALLOW=1 python3 scripts/export_snapshots_shenwan_v2.py

说明：申万行业指数无官方 60m/15m K 线；缺分钟数据时不写入快照（避免误导性默认值）。
      若需启用，须先同步各行业成分股 60m/15m 并完成等权合成（见 shenwan_sector_kline.py）。
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
