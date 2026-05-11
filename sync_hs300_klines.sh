#!/usr/bin/env bash
# 沪深300名单（backend/data/watchlist_hs300.json）：日线 + 60m + 15m 拉取。
# 在仓库根目录执行: ./sync_hs300_klines.sh
# 可透传参数，例如: ./sync_hs300_klines.sh --limit 5 --sleep 0.05

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT}/backend"

exec python3 "${ROOT}/backend/scripts/sync_hs300_kline_incremental.py" --daily "$@"
