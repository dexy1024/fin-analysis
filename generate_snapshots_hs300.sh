#!/usr/bin/env bash
# 沪深300 全量快照：读取 backend/data/watchlist_hs300.json，
# 追加写入 logs/snapshots_hs300_YYYY.csv（字段与自选 snapshots_YYYY.csv 一致）
# 用法：在项目根目录执行  ./generate_snapshots_hs300.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
SCRIPT="${BACKEND_DIR}/scripts/export_snapshots_hs300.py"

ENV_FILE="${ROOT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
fi

if [[ ! -x "${VENV_PY}" ]]; then
  echo "错误: 未找到可执行虚拟环境 ${VENV_PY}，请先创建 .venv 并安装依赖。" >&2
  exit 1
fi

if [[ ! -f "${SCRIPT}" ]]; then
  echo "错误: 未找到 ${SCRIPT}" >&2
  exit 1
fi

cd "${BACKEND_DIR}"
export FIN_SNAPSHOT_ALLOW=1
exec "${VENV_PY}" "${SCRIPT}"
