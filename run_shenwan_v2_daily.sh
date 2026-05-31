#!/usr/bin/env bash
# 申万二级 · 每日手动执行入口
#
# 设计说明（趋势 vs 可做 vs 60m）：docs/shenwan_v2_行业轮动流水线.md
#
# 默认：读取 shenwan_v2_sector_codes.json，按序生成下列 CSV（均含「数据日期」）
#
# 输出 CSV（9 个，按执行顺序）：
#   1. 01_shenwan_v2_analysis_result.csv                   — 当日量化打标快照（仅是否综合满足=1）
#   2. logs/02_shenwan_v2_analysis_history.csv               — 量化打标历史长表（按数据日期 upsert）
#   3. 03_shenwan_v2_volume_price_signals.csv                — 当日满足三项量价/资金面条件的行业快照
#   4. logs/04_shenwan_v2_volume_price_signals_history.csv   — 上述行业历史长表（按数据日期 upsert）
#   5. 05_shenwan_v2_crowding_monitor.csv                    — 拥挤度监控（120 日 Amount Share 分位打标）
#   6. 06_shenwan_v2_trend_sectors.csv                       — 三表合并趋势行业分层
#   7. 07_shenwan_v2_actionable_sectors.csv                  — 今日「可做」行业 Top3（轮动参与）
#   8. 08_shenwan_v2_sector_leaders.csv                      — 可做行业龙头个股（并写入 observation.json）
#   9. 09_shenwan_v2_buyable_e2e.csv                         — 端到端可买/盯盘（需已有 logs/snapshots_*_new.csv，否则跳过）
#
# 可选：先更新行业列表 + 成分股（较慢，仅行业变动或缺 JSON 时用）
#
# 在仓库根目录执行:
#   ./run_shenwan_v2_daily.sh                  # 日常：只更新 CSV
#   ./run_shenwan_v2_daily.sh --fetch-sectors  # 先抓行业/成分股，再更新 CSV
#   ./run_shenwan_v2_daily.sh --fetch-sectors --force
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

FETCH_SH="${ROOT}/fetch_shenwan_v2_sectors.sh"
PY_ANALYSIS="${ROOT}/backend/scripts/shenwan_v2_sector_analysis.py"
PY_VOLUME="${ROOT}/backend/scripts/shenwan_v2_volume_price_signals.py"
PY_CROWDING="${ROOT}/backend/scripts/shenwan_v2_crowding_monitor.py"
PY_TREND="${ROOT}/backend/scripts/shenwan_v2_trend_sectors.py"
PY_LEADERS="${ROOT}/backend/scripts/shenwan_v2_sector_leaders.py"
PY_PICK_E2E="${ROOT}/backend/scripts/pick_buyable_e2e.py"
JSON="${ROOT}/shenwan_v2_sector_codes.json"
LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"
CSV_ANALYSIS="${ROOT}/01_shenwan_v2_analysis_result.csv"
CSV_VOLUME="${ROOT}/03_shenwan_v2_volume_price_signals.csv"
CSV_ANALYSIS_HISTORY="${LOG_DIR}/02_shenwan_v2_analysis_history.csv"
CSV_VOLUME_HISTORY="${LOG_DIR}/04_shenwan_v2_volume_price_signals_history.csv"
CSV_CROWDING="${ROOT}/05_shenwan_v2_crowding_monitor.csv"
CSV_TREND="${ROOT}/06_shenwan_v2_trend_sectors.csv"
CSV_ACTIONABLE="${ROOT}/07_shenwan_v2_actionable_sectors.csv"
CSV_LEADERS="${ROOT}/08_shenwan_v2_sector_leaders.csv"

MAX_ROUNDS="${SHENWAN_ANALYSIS_MAX_ROUNDS:-6}"
COOLDOWN_SEC="${SHENWAN_ANALYSIS_COOLDOWN_SEC:-60}"
FETCH_SECTORS=0
FETCH_FORCE=0
EXTRA_ARGS=()

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --fetch-sectors) FETCH_SECTORS=1 ;;
    --force) FETCH_FORCE=1 ;;
    --max-rounds) MAX_ROUNDS="$2"; shift ;;
    --cooldown) COOLDOWN_SEC="$2"; shift ;;
    *) EXTRA_ARGS+=("$1") ;;
  esac
  shift
done

LOG_FILE="${LOG_DIR}/shenwan_v2_daily_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "===== 申万二级 · 每日任务 ====="
echo "开始: $(date '+%F %T')"
echo "日志: ${LOG_FILE}"

if [[ "${FETCH_SECTORS}" -eq 1 ]]; then
  echo ""
  echo "--- [可选] 更新行业列表与成分股 ---"
  fetch_args=()
  [[ "${FETCH_FORCE}" -eq 1 ]] && fetch_args+=(--force)
  "${FETCH_SH}" "${fetch_args[@]}"
fi

if [[ ! -f "${JSON}" ]]; then
  echo "错误: 未找到 ${JSON}"
  echo "请先执行: ./run_shenwan_v2_daily.sh --fetch-sectors"
  exit 1
fi

echo "输入: ${JSON}"
echo "快照: ${CSV_ANALYSIS}"
echo "      ${CSV_VOLUME}"
echo "      ${CSV_CROWDING}"
echo "长表: ${CSV_ANALYSIS_HISTORY}"
echo "      ${CSV_VOLUME_HISTORY}"

PY_ARGS=(--analysis-only --workers 1 -o "${ROOT}")
if ((${#EXTRA_ARGS[@]} > 0)); then
  PY_ARGS+=("${EXTRA_ARGS[@]}")
fi

run_analysis_with_retry() {
  local round=1
  while [[ "${round}" -le "${MAX_ROUNDS}" ]]; do
    echo ""
    echo "--- [1/6] 量化打标 第 ${round}/${MAX_ROUNDS} 轮 → CSV 1-2 ---"
    if python3 "${PY_ANALYSIS}" "${PY_ARGS[@]}"; then
      if [[ -f "${CSV_ANALYSIS}" ]]; then
        local lines
        lines="$(wc -l < "${CSV_ANALYSIS}" | tr -d ' ')"
        if [[ "${lines}" -ge 2 ]]; then
          echo "打标完成: ${lines} 行（含表头）→ ${CSV_ANALYSIS}"
          return 0
        fi
        echo "警告: ${CSV_ANALYSIS} 行数不足（${lines}），视为失败并重试"
      else
        echo "警告: 未生成 ${CSV_ANALYSIS}"
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
  return 1
}

run_volume_signals() {
  echo ""
  echo "--- [2/6] 量价资金面信号 → CSV 3-4 ---"
  python3 "${PY_VOLUME}" --workers 1 -o "${ROOT}"
}

run_crowding_monitor() {
  echo ""
  echo "--- [3/6] 拥挤度监控 → CSV 5 ---"
  python3 "${PY_CROWDING}" --workers 1 -o "${ROOT}"
}

run_trend_sectors() {
  echo ""
  echo "--- [4/6] 趋势行业综合 → CSV 6-7 ---"
  python3 "${PY_TREND}" -o "${ROOT}"
}

if ! run_analysis_with_retry; then
  echo "失败: 量化打标已达最大轮次 ${MAX_ROUNDS}"
  exit 1
fi

if ! run_volume_signals; then
  echo "失败: 量价信号脚本退出非 0"
  exit 1
fi

if ! run_crowding_monitor; then
  echo "失败: 拥挤度监控脚本退出非 0"
  exit 1
fi

run_sector_leaders() {
  echo ""
  echo "--- [5/6] 战术个股龙头 → CSV 8 + observation ---"
  python3 "${PY_LEADERS}" --workers 6 -o "${ROOT}"
}

if ! run_trend_sectors; then
  echo "失败: 趋势行业综合脚本退出非 0"
  exit 1
fi

if ! run_sector_leaders; then
  echo "失败: 战术个股龙头脚本退出非 0"
  exit 1
fi

run_pick_buyable_e2e() {
  echo ""
  echo "--- [6/6] 端到端可买清单 → CSV 9（申万 + snapshots）---"
  if compgen -G "${LOG_DIR}/snapshots_"*_new.csv" > /dev/null 2>&1; then
    python3 "${PY_PICK_E2E}" -o "${ROOT}" || echo "警告: 端到端清单生成失败（可稍后 ./pick_buyable_e2e.sh）"
  else
    echo "跳过: 未找到 ${LOG_DIR}/snapshots_*_new.csv"
    echo "      请先 ./generate_snapshots.sh --write 再执行 ./pick_buyable_e2e.sh"
  fi
}

run_pick_buyable_e2e

if [[ ! -f "${CSV_VOLUME}" ]]; then
  echo "失败: 未生成 ${CSV_VOLUME}"
  exit 1
fi

vol_lines="$(wc -l < "${CSV_VOLUME}" | tr -d ' ')"
hist_analysis_lines=0
hist_volume_lines=0
[[ -f "${CSV_ANALYSIS_HISTORY}" ]] && hist_analysis_lines="$(wc -l < "${CSV_ANALYSIS_HISTORY}" | tr -d ' ')"
[[ -f "${CSV_VOLUME_HISTORY}" ]] && hist_volume_lines="$(wc -l < "${CSV_VOLUME_HISTORY}" | tr -d ' ')"
echo ""
echo "完成: $(date '+%F %T')"
echo "  快照 ${CSV_ANALYSIS}  ($(wc -l < "${CSV_ANALYSIS}" | tr -d ' ') 行，含表头)"
echo "  快照 ${CSV_VOLUME}     (${vol_lines} 行，含表头；满足三项条件的行业)"
echo "  快照 ${CSV_CROWDING}  ($(wc -l < "${CSV_CROWDING}" | tr -d ' ') 行，含表头；拥挤度打标)"
echo "  快照 ${CSV_TREND}       ($(wc -l < "${CSV_TREND}" | tr -d ' ') 行，含表头；趋势行业分层)"
if [[ -f "${CSV_ACTIONABLE}" ]]; then
  echo "  快照 ${CSV_ACTIONABLE} ($(wc -l < "${CSV_ACTIONABLE}" | tr -d ' ') 行，含表头；今日可做)"
fi
if [[ -f "${CSV_LEADERS}" ]]; then
  echo "  快照 ${CSV_LEADERS}  ($(wc -l < "${CSV_LEADERS}" | tr -d ' ') 行，含表头；行业龙头)"
fi
CSV_E2E="${ROOT}/09_shenwan_v2_buyable_e2e.csv"
if [[ -f "${CSV_E2E}" ]]; then
  echo "  快照 ${CSV_E2E} ($(wc -l < "${CSV_E2E}" | tr -d ' ') 行，含表头；端到端可买/盯盘)"
fi
echo "  长表 ${CSV_ANALYSIS_HISTORY}  (${hist_analysis_lines} 行，含表头)"
echo "  长表 ${CSV_VOLUME_HISTORY}  (${hist_volume_lines} 行，含表头)"
