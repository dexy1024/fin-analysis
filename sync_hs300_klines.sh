#!/usr/bin/env bash
# 沪深300（watchlist_hs300.json）：daily + 60m + 15m 全量同步，未齐则自动补跑重试。
# 在仓库根目录执行: ./sync_hs300_klines.sh
# 先预览: ./sync_hs300_klines.sh --dry-run
# 只补未齐: ./sync_hs300_klines.sh --stale-only
# 可透传参数，例如: ./sync_hs300_klines.sh --sleep 1.5 --max-rounds 15

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT}/backend"

exec python3 "${ROOT}/backend/scripts/sync_hs300_kline_incremental.py" --until-fresh --sleep 1 "$@"
