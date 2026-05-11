"""作战指令引擎运行审计：每次进入引擎即追加一行 JSON 到 logs/snapshot_engine_runs.log。"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[2]
_LOG_PATH = _ROOT / "logs" / "snapshot_engine_runs.log"
_TZ_SH = ZoneInfo("Asia/Shanghai")


def snapshot_write_allowed() -> bool:
    """仅当显式 FIN_SNAPSHOT_ALLOW=1|true|yes|on 时允许写入 snapshots CSV。"""
    return os.environ.get("FIN_SNAPSHOT_ALLOW", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def assert_snapshot_write_allowed() -> None:
    if snapshot_write_allowed():
        return
    raise SystemExit(
        "已拒绝写入 snapshots CSV：未设置 FIN_SNAPSHOT_ALLOW=1。"
        "自选写盘：./generate_snapshots.sh --write 或 cd backend && python run_trade_command.py --write；"
        "HS300：./generate_snapshots_hs300.sh --write"
    )


def _argv_shell_quoted() -> str:
    """便于肉眼从日志复制整条命令。"""
    if not sys.argv:
        return ""
    try:
        return shlex.join(sys.argv)
    except (TypeError, ValueError):
        return " ".join(shlex.quote(str(a)) for a in sys.argv)


def _parent_process_command(ppid: int) -> str | None:
    """macOS / Linux：取父进程命令行，便于定位 cron / launchd / 终端包装脚本。"""
    if ppid <= 1:
        return None
    for cmd in (
        ["ps", "-p", str(ppid), "-o", "command="],
        ["ps", "-p", str(ppid), "-o", "args="],
    ):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            out = (proc.stdout or "").strip()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def log_snapshot_engine_run(kind: str, generate_report: bool, symbol_count: int) -> None:
    ppid = os.getppid()
    rec: dict = {
        "ts": datetime.now(_TZ_SH).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "ppid": ppid,
        "user": getpass.getuser(),
        "kind": kind,
        "generate_report": generate_report,
        "symbol_count": symbol_count,
        "argv": sys.argv,
        "argv_shell": _argv_shell_quoted(),
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "parent_command": _parent_process_command(ppid),
    }
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        # 审计失败不得影响作战引擎主流程
        pass
