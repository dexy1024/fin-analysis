#!/usr/bin/env bash
# 梅花2test（889999）演示「未来 K」：后端进程内启用 MEIHUA2TEST_FUTURE_K，再按 restart_services.sh 启停。
#
# 用法（项目根目录）:
#   ./restart_services_meihua2test_future.sh
# 跳过夹具重建（仅重启并带环境变量）:
#   SKIP_MEIHUA2TEST_BUILD=1 ./restart_services_meihua2test_future.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MEIHUA2TEST_FUTURE_K=1

if [[ "${SKIP_MEIHUA2TEST_BUILD:-}" != "1" ]] && [[ -f "${ROOT_DIR}/backend/data/kline_60_600873.csv" ]]; then
  echo "[meihua2test] MEIHUA2TEST_FUTURE_K=1，重建 889999 夹具..."
  (cd "${ROOT_DIR}/backend" && python3 scripts/build_meihua2test_fixture.py)
else
  echo "[meihua2test] 跳过夹具重建（无 600873 源 CSV 或已设 SKIP_MEIHUA2TEST_BUILD=1）"
fi

echo "[meihua2test] 启动后端/前端（后端已带 MEIHUA2TEST_FUTURE_K=1）..."
# shellcheck source=restart_services.sh
source "${ROOT_DIR}/restart_services.sh"
