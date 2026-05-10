"""作战指令引擎运行审计：每次进入引擎即追加一行 JSON 到 logs/snapshot_engine_runs.log。"""

from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[2]
_LOG_PATH = _ROOT / "logs" / "snapshot_engine_runs.log"
_TZ_SH = ZoneInfo("Asia/Shanghai")


def log_snapshot_engine_run(kind: str, generate_report: bool, symbol_count: int) -> None:
    rec = {
        "ts": datetime.now(_TZ_SH).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "user": getpass.getuser(),
        "kind": kind,
        "generate_report": generate_report,
        "symbol_count": symbol_count,
        "argv": sys.argv,
    }
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
