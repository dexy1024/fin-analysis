#!/usr/bin/env bash
# 兼容入口：已合并至 run_shenwan_v2_daily.sh --e2e-only
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ROOT}/run_shenwan_v2_daily.sh" --e2e-only "$@"
