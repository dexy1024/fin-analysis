#!/usr/bin/env python3
"""
将 backend/data/watchlist_hs300.json 全量跑出与 logs/snapshots_YYYY.csv
同字段的快照行，写入 logs/snapshots_hs300_YYYY.csv。

用法（仓库根目录）：
    PYTHONPATH=backend python3 backend/scripts/export_snapshots_hs300.py

或：
    cd backend && python3 scripts/export_snapshots_hs300.py

（不向 Web 前端注册；仅在本地产出 CSV）

诊断落盘与自选快照共用 logs/snapshot_trace_latest.log（15m：h15背驰 前缀；
区间对齐：price_align 前缀），由 FIN_SNAPSHOT_TRACE_VERBOSE 与 FIN_HS300_SNAPSHOT_VERBOSE 共同决定
是否挂载（任一为开即挂载并写 h15/对齐行；见 trade_command_engine._run_trade_command_engine_core）。
"""

from __future__ import annotations

import logging
import os
import sys

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from pathlib import Path  # noqa: E402

ROOT_DIR = Path(backend_dir).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)

from utils.csv_logger import SNAPSHOT_TRACE_LOG  # noqa: E402
from services.trade_command_engine import export_hs300_snapshots_to_csv  # noqa: E402


def main() -> int:
    v = (os.environ.get("FIN_HS300_SNAPSHOT_VERBOSE") or "1").strip().lower()
    on = v not in ("0", "false", "no", "off")
    logging.info(
        "export_snapshots_hs300: FIN_HS300_SNAPSHOT_VERBOSE=%r → stderr 逐条 15m trace=%s",
        os.environ.get("FIN_HS300_SNAPSHOT_VERBOSE", ""),
        "开" if on else "关",
    )
    logging.info(
        "export_snapshots_hs300: 与自选快照共用诊断文件 %s（FIN_SNAPSHOT_TRACE_VERBOSE 控制是否落盘 price_align；"
        "与 FIN_HS300_SNAPSHOT_VERBOSE 任一为开即挂载）",
        SNAPSHOT_TRACE_LOG,
    )
    path = export_hs300_snapshots_to_csv()
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
