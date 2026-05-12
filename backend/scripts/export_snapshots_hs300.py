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
from datetime import datetime
from pathlib import Path

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

ROOT_DIR = Path(backend_dir).resolve().parent
HS300_H15_TRACE_LOG = ROOT_DIR / "logs" / "hs300_h15_trace_latest.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)

from services.trade_command_engine import export_hs300_snapshots_to_csv  # noqa: E402


class _H15TraceLogFilter(logging.Filter):
    """仅落盘含「h15背驰」的 INFO 行（与引擎里逐条 trace 前缀一致）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return "h15背驰" in record.getMessage()


def _attach_h15_trace_file_handler() -> None:
    HS300_H15_TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HS300_H15_TRACE_LOG.open("a", encoding="utf-8") as f:
        f.write(
            f"\n{'=' * 72}\n"
            f"# HS300 15m 背驰 trace 批次开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
    fh = logging.FileHandler(HS300_H15_TRACE_LOG, mode="a", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    fh.addFilter(_H15TraceLogFilter())
    logging.getLogger().addHandler(fh)


def main() -> int:
    v = (os.environ.get("FIN_HS300_SNAPSHOT_VERBOSE") or "1").strip().lower()
    on = v not in ("0", "false", "no", "off")
    logging.info(
        "export_snapshots_hs300: FIN_HS300_SNAPSHOT_VERBOSE=%r → 仅输出 15m 背驰逐条 trace=%s（0/false/off 关闭）",
        os.environ.get("FIN_HS300_SNAPSHOT_VERBOSE", ""),
        "开" if on else "关",
    )
    if on:
        _attach_h15_trace_file_handler()
        logging.info(
            "export_snapshots_hs300: 15m 背驰 trace 同时追加写入 %s",
            HS300_H15_TRACE_LOG,
        )
    path = export_hs300_snapshots_to_csv()
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
