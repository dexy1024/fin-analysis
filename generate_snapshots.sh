#!/usr/bin/env bash
# 自选/观测池快照：追加写入 logs/snapshots_YYYY.csv（默认不写作战指令 Markdown）
# 用法：在项目根目录执行  ./generate_snapshots.sh
# 可选：./generate_snapshots.sh --report   同时生成 trade_reports/作战指令_*.md

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_PY="${ROOT_DIR}/.venv/bin/python"

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

cd "${BACKEND_DIR}"
exec "${VENV_PY}" run_trade_command.py "$@"
