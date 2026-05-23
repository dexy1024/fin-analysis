#!/usr/bin/env bash
# 申万二级行业打标：读取 shenwan_v2_sectors.json 的 sector_code / sector_name，
# 拉取行业与沪深300 K 线并计算指标 → shenwan_v2_analysis_result.csv
# 不抓取成分股；顺序执行（workers=1），外层带重试。
#
# 在仓库根目录执行:
#   ./run_shenwan_v2_analysis.sh
#   ./run_shenwan_v2_analysis.sh --max-rounds 5 --cooldown 60
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

PY="${ROOT}/backend/scripts/shenwan_v2_sector_analysis.py"
JSON="${ROOT}/shenwan_v2_sectors.json"
CSV="${ROOT}/shenwan_v2_analysis_result.csv"
LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"

MAX_ROUNDS="${SHENWAN_ANALYSIS_MAX_ROUNDS:-6}"
COOLDOWN_SEC="${SHENWAN_ANALYSIS_COOLDOWN_SEC:-60}"
EXTRA_ARGS=()

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --max-rounds) MAX_ROUNDS="$2"; shift ;;
    --cooldown) COOLDOWN_SEC="$2"; shift ;;
    *) EXTRA_ARGS+=("$1") ;;
  esac
  shift
done

if [[ ! -f "${JSON}" ]]; then
  echo "错误: 未找到 ${JSON}，请先执行 ./fetch_shenwan_v2_sectors.sh"
  exit 1
fi

LOG_FILE="${LOG_DIR}/shenwan_v2_analysis_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "===== 申万二级行业量化打标 ====="
echo "开始: $(date '+%F %T')"
echo "日志: ${LOG_FILE}"
echo "输入: ${JSON}"
echo "输出: ${CSV}"

PY_ARGS=(--analysis-only --workers 1 -o "${ROOT}")
PY_ARGS+=("${EXTRA_ARGS[@]}")

round=1
while [[ "${round}" -le "${MAX_ROUNDS}" ]]; do
  echo ""
  echo "--- 第 ${round}/${MAX_ROUNDS} 轮 ---"
  if python3 "${PY}" "${PY_ARGS[@]}"; then
    if [[ -f "${CSV}" ]]; then
      lines="$(wc -l < "${CSV}" | tr -d ' ')"
      if [[ "${lines}" -ge 2 ]]; then
        echo "完成: $(date '+%F %T')，结果 ${lines} 行（含表头）→ ${CSV}"
        exit 0
      fi
      echo "警告: ${CSV} 行数不足（${lines}），视为失败并重试"
    else
      echo "警告: 未生成 ${CSV}"
    fi
  else
    echo "本轮 Python 退出非 0"
  fi

  if [[ "${round}" -ge "${MAX_ROUNDS}" ]]; then
    break
  fi
  echo "等待 ${COOLDOWN_SEC}s 后重试…"
  sleep "${COOLDOWN_SEC}"
  round=$((round + 1))
done

echo "失败: 已达最大轮次 ${MAX_ROUNDS}"
exit 1
