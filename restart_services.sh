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
# 杀死占用端口的进程
lsof -ti:${BACKEND_PORT} | xargs kill -9 2>/dev/null || true
lsof -ti:${FRONTEND_PORT} | xargs kill -9 2>/dev/null || true
# 额外清理 vite 和 uvicorn 进程
pkill -9 -f "vite" 2>/dev/null || true
pkill -9 -f "uvicorn.*main:app.*${BACKEND_PORT}" 2>/dev/null || true
pkill -9 -f "node.*frontend" 2>/dev/null || true

sleep 2

# 确认端口已释放
for port in ${BACKEND_PORT} ${FRONTEND_PORT}; do
  if lsof -ti:${port} >/dev/null 2>&1; then
    echo "ERROR: Port ${port} still in use after kill"
    exit 1
  fi
done
echo "Ports cleared successfully"

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
echo "Waiting for services to start..."
sleep 3

# 验证服务是否成功启动
echo
echo "Checking services..."
backend_ok=false
frontend_ok=false

if curl -s http://127.0.0.1:${BACKEND_PORT}/api/diagnosis/defense-radar/summary >/dev/null 2>&1; then
  backend_ok=true
  echo "✓ Backend: OK (http://127.0.0.1:${BACKEND_PORT})"
else
  echo "✗ Backend: Failed to start"
fi

if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:${FRONTEND_PORT}/ | grep -q "200"; then
  frontend_ok=true
  echo "✓ Frontend: OK (http://127.0.0.1:${FRONTEND_PORT})"
else
  echo "✗ Frontend: Failed to start"
fi

echo
if [ "${backend_ok}" = true ] && [ "${frontend_ok}" = true ]; then
  echo "Services restarted successfully."
  echo "Backend PID:  ${backend_pid}"
  echo "Frontend PID: ${frontend_pid}"
  echo "Backend log:  ${backend_log}"
  echo "Frontend log: ${frontend_log}"
else
  echo "WARNING: Some services failed to start. Check logs:"
  echo "Backend log:  ${backend_log}"
  echo "Frontend log: ${frontend_log}"
  exit 1
fi
