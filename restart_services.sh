#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"
LOG_DIR="${ROOT_DIR}/logs"

BACKEND_PORT=8000
FRONTEND_PORT=5173

mkdir -p "${LOG_DIR}"

# 兼容 pip --user 安装路径（例如 uvicorn 在 ~/Library/Python/x.y/bin）
for py_ver in 3.13 3.12 3.11 3.10 3.9; do
  user_bin="${HOME}/Library/Python/${py_ver}/bin"
  if [ -d "${user_bin}" ]; then
    export PATH="${user_bin}:${PATH}"
  fi
done
if [ -d "${HOME}/.local/bin" ]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi

# 选用已安装 uvicorn 的 Python（系统自带 python3 常未装依赖）
pick_python_with_uvicorn() {
  if [ -n "${PYTHON_BIN:-}" ] && [ -x "${PYTHON_BIN}" ] && "${PYTHON_BIN}" -m uvicorn --version >/dev/null 2>&1; then
    echo "${PYTHON_BIN}"
    return 0
  fi
  for cand in python3 python3.13 python3.12 python3.11; do
    if command -v "${cand}" >/dev/null 2>&1 && "${cand}" -m uvicorn --version >/dev/null 2>&1; then
      command -v "${cand}"
      return 0
    fi
  done
  echo "python3"
  return 0
}

PYTHON_FOR_BACKEND="$(pick_python_with_uvicorn)"

echo "Stopping existing services on ports ${BACKEND_PORT}/${FRONTEND_PORT}..."
lsof -ti:${BACKEND_PORT} | xargs kill -9 2>/dev/null || true
lsof -ti:${FRONTEND_PORT} | xargs kill -9 2>/dev/null || true

sleep 1

timestamp="$(date +"%Y%m%d_%H%M%S")"
backend_log="${LOG_DIR}/backend_${timestamp}.log"
frontend_log="${LOG_DIR}/frontend_${timestamp}.log"

echo "Starting backend on port ${BACKEND_PORT} (python: ${PYTHON_FOR_BACKEND})..."
nohup bash -lc "cd \"${BACKEND_DIR}\" && \"${PYTHON_FOR_BACKEND}\" -m uvicorn main:app --host 127.0.0.1 --port ${BACKEND_PORT}" >"${backend_log}" 2>&1 &
backend_pid=$!

echo "Starting frontend on port ${FRONTEND_PORT}..."
nohup bash -lc "cd \"${FRONTEND_DIR}\" && npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT}" >"${frontend_log}" 2>&1 &
frontend_pid=$!

echo
echo "Services restarted successfully."
echo "Backend PID:  ${backend_pid}"
echo "Frontend PID: ${frontend_pid}"
echo "Backend URL:  http://127.0.0.1:${BACKEND_PORT}"
echo "Frontend URL: http://127.0.0.1:${FRONTEND_PORT}"
echo "Backend log:  ${backend_log}"
echo "Frontend log: ${frontend_log}"
