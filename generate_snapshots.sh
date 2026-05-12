#!/usr/bin/env bash
# 自选/观测池快照：本脚本默认追加写入 logs/snapshots_YYYY_new.csv（与旧 snapshots_YYYY.csv 分离）。
# 须显式传 --write / -w，避免后台循环或误点脚本即写盘；不读取 .env 中的 FIN_SNAPSHOT_ALLOW 作为放行条件。
# 用法（项目根目录）：
#   ./generate_snapshots.sh --write
#   ./generate_snapshots.sh --write --report
# 写回旧文件名：  FIN_SNAPSHOT_CSV_SUFFIX=  ./generate_snapshots.sh --write
# 直接调 Python:  cd backend && .venv/bin/python run_trade_command.py --write
# 需要 Markdown 报告时在末尾再加 --report（不要输入方括号）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_PY="${ROOT_DIR}/.venv/bin/python"

WRITE=0
PY_ARGS=()
for a in "$@"; do
  case "$a" in
    --write | -w)
      WRITE=1
      ;;
    *)
      PY_ARGS+=("$a")
      ;;
  esac
done

ENV_FILE="${ROOT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
fi

if [[ "${WRITE}" -eq 0 ]]; then
  echo "未加 --write，本脚本不会写 snapshots（防误触发与定时任务）。写盘请执行:" >&2
  echo "  ./generate_snapshots.sh --write                # 仅 CSV → logs/snapshots_YYYY_new.csv" >&2
  echo "  ./generate_snapshots.sh --write --report       # CSV + 作战指令 Markdown（zsh 勿写方括号）" >&2
  exit 2
fi

export FIN_SNAPSHOT_ALLOW=1
# 快照诊断（15m 背驰 + 区间对齐）落盘 logs/snapshot_trace_latest.log；未设置环境变量时默认开启
if ! printenv FIN_SNAPSHOT_TRACE_VERBOSE >/dev/null 2>&1; then
  export FIN_SNAPSHOT_TRACE_VERBOSE=1
fi
# 与旧 logs/snapshots_YYYY.csv 分离；仅在「未出现在环境中」时默认 _new（显式 FIN_SNAPSHOT_CSV_SUFFIX= 空可写回旧名）
if ! printenv FIN_SNAPSHOT_CSV_SUFFIX >/dev/null 2>&1; then
  export FIN_SNAPSHOT_CSV_SUFFIX=_new
fi

if [[ ! -x "${VENV_PY}" ]]; then
  echo "错误: 未找到可执行虚拟环境 ${VENV_PY}，请先创建 .venv 并安装依赖。" >&2
  exit 1
fi

cd "${BACKEND_DIR}"
echo "自选快照：FIN_SNAPSHOT_TRACE_VERBOSE=${FIN_SNAPSHOT_TRACE_VERBOSE:-}（0=关）→ stderr + logs/snapshot_trace_latest.log" >&2
# set -u 下空数组 "${PY_ARGS[@]}" 在部分 bash 会报 unbound variable，须分支展开
if ((${#PY_ARGS[@]} > 0)); then
  exec "${VENV_PY}" run_trade_command.py --write "${PY_ARGS[@]}"
else
  exec "${VENV_PY}" run_trade_command.py --write
fi
