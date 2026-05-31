#!/bin/bash
# 手动更新 上证指数 + watchlist.json + observation.json + observation_hk.json 的 K 线
# - 始终：60m / 15m
# - 日线：仅北京时间 16:00 及之后执行时才拉（当日收盘后；可用 --force-daily 强制）
# 慢速拉取防新浪限流；不含 DEFENSE_RADAR 雷达核心列表（由 kline_scheduler 定时跑）
# 默认：标的间隔 8s、周期间隔 5s、最多 5 轮（遇 456 可再加大 --sleep）
#
# 用法:
#   ./update_data.sh
#   ./update_data.sh --sleep 12 --period-sleep 8
#   ./update_data.sh --sleep 12 --max-rounds 6
#   ./update_data.sh --force-daily   # 16 点前也拉日线

set -euo pipefail
cd "$(dirname "$0")" || exit 1

echo "========================================"
echo "上证 + watchlist + observation + observation_hk K 线同步（慢速）"
echo "时间: $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z')"
echo "========================================"

/usr/bin/python3 update_data.py "$@"
rc=$?

echo ""
echo "========================================"
if [ "$rc" -eq 0 ]; then
  echo "执行完成"
else
  echo "执行完成（部分标的未齐，exit=$rc）"
fi
echo "========================================"

exit "$rc"
