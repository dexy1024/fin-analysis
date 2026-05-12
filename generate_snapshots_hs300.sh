#!/usr/bin/env bash
# 沪深300 全量快照：读取 backend/data/watchlist_hs300.json，
# 追加写入 logs/snapshots_hs300_YYYY.csv。
# 须显式传 --write / -w，避免定时任务误调用即写盘。
#
# 详细排查：15m 背驰（h15背驰）+ 区间对齐（price_align）→ stderr，并追加 logs/snapshot_trace_latest.log
#   挂载条件：FIN_SNAPSHOT_TRACE_VERBOSE 或 FIN_HS300_SNAPSHOT_VERBOSE 任一为开（未设置时脚本内默认均为 1）。
#   关闭落盘/减少输出：  FIN_SNAPSHOT_TRACE_VERBOSE=0 FIN_HS300_SNAPSHOT_VERBOSE=0 ./generate_snapshots_hs300.sh --write
# 用法（项目根目录）：  ./generate_snapshots_hs300.sh --write

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
SCRIPT="${BACKEND_DIR}/scripts/export_snapshots_hs300.py"

WRITE=0
for a in "$@"; do
  case "$a" in
    --write | -w)
      WRITE=1
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
  echo "未加 --write，本脚本不会写 HS300 快照。写盘请执行:" >&2
  echo "  ./generate_snapshots_hs300.sh --write" >&2
  exit 2
fi

export FIN_SNAPSHOT_ALLOW=1
if ! printenv FIN_SNAPSHOT_TRACE_VERBOSE >/dev/null 2>&1; then
  export FIN_SNAPSHOT_TRACE_VERBOSE=1
fi
# 详细日志默认开；仅当环境中已显式设为 0/false/off 时不覆盖
if [[ -z "${FIN_HS300_SNAPSHOT_VERBOSE+x}" ]]; then
  export FIN_HS300_SNAPSHOT_VERBOSE=1
fi
export PYTHONUNBUFFERED=1

if [[ ! -x "${VENV_PY}" ]]; then
  echo "错误: 未找到可执行虚拟环境 ${VENV_PY}，请先创建 .venv 并安装依赖。" >&2
  exit 1
fi

if [[ ! -f "${SCRIPT}" ]]; then
  echo "错误: 未找到 ${SCRIPT}" >&2
  exit 1
fi

cd "${BACKEND_DIR}"
echo "HS300 快照：FIN_HS300_SNAPSHOT_VERBOSE=${FIN_HS300_SNAPSHOT_VERBOSE:-} FIN_SNAPSHOT_TRACE_VERBOSE=${FIN_SNAPSHOT_TRACE_VERBOSE:-}（0=关）→ stderr + logs/snapshot_trace_latest.log" >&2
exec "${VENV_PY}" "${SCRIPT}"
