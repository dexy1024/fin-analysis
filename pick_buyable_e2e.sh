#!/usr/bin/env bash
# 端到端：申万 daily 产出 + 观察池快照 → 可买/盯盘清单
#
# 前置：
#   ./run_shenwan_v2_daily.sh
#   ./generate_snapshots.sh --write    # 更新 logs/snapshots_*_new.csv
#
# 用法（仓库根目录）：
#   ./pick_buyable_e2e.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"
python3 "${ROOT}/backend/scripts/pick_buyable_e2e.py" -o "${ROOT}"
