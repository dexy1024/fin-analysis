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

# 加载项目级环境变量（.env 中放置敏感配置）
ENV_FILE="${ROOT_DIR}/.env"
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
fi

# 设置代理（Clash Verge）
export HTTP_PROXY="http://127.0.0.1:7897"
export HTTPS_PROXY="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"
export https_proxy="http://127.0.0.1:7897"

# 选用已安装 uvicorn 的 Python（系统自带 python3 常未装依赖）
pick_python_with_uvicorn() {
  if [ -n "${PYTHON_BIN:-}" ] && [ -x "${PYTHON_BIN}" ] && "${PYTHON_BIN}" -m uvicorn --version >/dev/null 2>&1; then
    echo "${PYTHON_BIN}"
    return 0
  fi
  # 优先使用项目虚拟环境
  venv_python="${ROOT_DIR}/.venv/bin/python"
  if [ -x "${venv_python}" ] && "${venv_python}" -m uvicorn --version >/dev/null 2>&1; then
    echo "${venv_python}"
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

# 优雅关闭进程：先 SIGTERM(-15)，超时后再 SIGKILL(-9)
graceful_kill_port() {
  local port=$1
  local pids
  pids=$(lsof -ti:${port} 2>/dev/null || true)
  if [ -n "${pids}" ]; then
    echo "Sending SIGTERM to processes on port ${port}: ${pids}"
    echo "${pids}" | xargs kill -15 2>/dev/null || true
    # 等待最多 5 秒让进程优雅退出
    for i in 1 2 3 4 5; do
      sleep 1
      pids=$(lsof -ti:${port} 2>/dev/null || true)
      if [ -z "${pids}" ]; then
        echo "Port ${port} cleared gracefully"
        return 0
      fi
    done
    # 仍有残留，强制 SIGKILL
    echo "Force killing remaining processes on port ${port}: ${pids}"
    echo "${pids}" | xargs kill -9 2>/dev/null || true
  fi
}

graceful_kill_pattern() {
  local pattern=$1
  local pids
  pids=$(pgrep -f "${pattern}" 2>/dev/null || true)
  if [ -n "${pids}" ]; then
    echo "${pids}" | xargs kill -15 2>/dev/null || true
    sleep 2
    pids=$(pgrep -f "${pattern}" 2>/dev/null || true)
    if [ -n "${pids}" ]; then
      echo "${pids}" | xargs kill -9 2>/dev/null || true
    fi
  fi
}

echo "Stopping existing services on ports ${BACKEND_PORT}/${FRONTEND_PORT}..."
# 优雅关闭占用端口的进程
graceful_kill_port ${BACKEND_PORT}
graceful_kill_port ${FRONTEND_PORT}
# 额外清理（使用包含项目路径的精确匹配，避免误杀其他项目）
graceful_kill_pattern "vite.*${FRONTEND_DIR}"
graceful_kill_pattern "uvicorn.*main:app.*${BACKEND_PORT}"
graceful_kill_pattern "gunicorn.*main:app.*${BACKEND_PORT}"
graceful_kill_pattern "node.*${FRONTEND_DIR}"

sleep 1

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

# 检查邮件通知环境变量配置
if [ -z "${EMAIL_SENDER:-}" ] || [ -z "${EMAIL_PASSWORD:-}" ] || [ -z "${EMAIL_RECIPIENT:-}" ]; then
  echo
  echo "【提示】邮件通知功能未配置，14:46 快照后将不会推送邮件。"
  echo "      如需启用，请在启动前设置以下环境变量："
  echo "      export EMAIL_SENDER=\"your_qq@qq.com\""
  echo "      export EMAIL_PASSWORD=\"your_qq_auth_code\""
  echo "      export EMAIL_RECIPIENT=\"recipient@example.com\""
  echo
fi

echo "Starting backend on port ${BACKEND_PORT} (python: ${PYTHON_FOR_BACKEND}, workers: 2)..."
nohup bash -lc "cd \"${BACKEND_DIR}\" && \"${PYTHON_FOR_BACKEND}\" -m gunicorn main:app -w 2 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:${BACKEND_PORT} --timeout 120 --access-logfile -" >"${backend_log}" 2>&1 &
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
