#!/usr/bin/env bash
# 已合并至 sync_hs300_klines.sh；此脚本保留为兼容入口（跳过全量，只补未齐并重试）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ROOT}/sync_hs300_klines.sh" --stale-only "$@"
