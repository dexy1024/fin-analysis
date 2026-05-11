#!/usr/bin/env bash
# 自选/观测池快照：追加写入 logs/snapshots_YYYY.csv
# 须显式传 --write / -w，避免后台循环或误点脚本即写盘；不读取 .env 中的 FIN_SNAPSHOT_ALLOW 作为放行条件。
# 用法（项目根目录）：
#   ./generate_snapshots.sh --write
#   ./generate_snapshots.sh --write --report
# 直接调 Python 写盘仍可用:  cd backend && FIN_SNAPSHOT_ALLOW=1 .venv/bin/python run_trade_command.py

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
  echo "  ./generate_snapshots.sh --write [--report]" >&2
  exit 2
fi

export FIN_SNAPSHOT_ALLOW=1

if [[ ! -x "${VENV_PY}" ]]; then
  echo "错误: 未找到可执行虚拟环境 ${VENV_PY}，请先创建 .venv 并安装依赖。" >&2
  exit 1
fi

cd "${BACKEND_DIR}"
exec "${VENV_PY}" run_trade_command.py "${PY_ARGS[@]}"
