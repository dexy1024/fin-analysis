#!/usr/bin/env bash
# 申万二级行业快照：读取 backend/data/observation_shenwan_v2.json，
# 追加写入 logs/snapshots_shenwan_v2_YYYY.csv（表头同 snapshots_YYYY_new.csv）。
# 须显式传 --write / -w。
#
# 用法（项目根目录）：
#   ./generate_snapshots_shenwan_v2.sh --write

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
SCRIPT="${BACKEND_DIR}/scripts/export_snapshots_shenwan_v2.py"

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
  echo "未加 --write，本脚本不会写申万行业快照。写盘请执行:" >&2
  echo "  ./generate_snapshots_shenwan_v2.sh --write" >&2
  exit 2
fi

export FIN_SNAPSHOT_ALLOW=1
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
exec "${VENV_PY}" "${SCRIPT}"
