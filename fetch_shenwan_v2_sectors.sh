#!/usr/bin/env bash
# 申万二级：抓取行业列表 + 成分股 → shenwan_v2_sectors.json（并同步 shenwan_v2_sector_codes.json）
# 偏稳定：慢速请求、Python 内重试 + 本脚本外层重试，直至全部行业都有 stocks。
#
# 在仓库根目录执行:
#   ./fetch_shenwan_v2_sectors.sh              # 补抓缺失成分股（推荐日常）
#   ./fetch_shenwan_v2_sectors.sh --force      # 强制重抓行业列表与全部成分股
#   ./fetch_shenwan_v2_sectors.sh --max-rounds 5 --cooldown 90
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

if [[ -x "${ROOT}/.venv/bin/python3" ]]; then
  PYTHON="${ROOT}/.venv/bin/python3"
else
  PYTHON="python3"
fi

PY="${ROOT}/backend/scripts/shenwan_v2_sector_analysis.py"
JSON="${ROOT}/shenwan_v2_sectors.json"
LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"

MAX_ROUNDS="${SHENWAN_FETCH_MAX_ROUNDS:-12}"
COOLDOWN_SEC="${SHENWAN_FETCH_COOLDOWN_SEC:-90}"
FORCE=0
EXTRA_ARGS=()

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --force) FORCE=1 ;;
    --max-rounds) MAX_ROUNDS="$2"; shift ;;
    --cooldown) COOLDOWN_SEC="$2"; shift ;;
    *) EXTRA_ARGS+=("$1") ;;
  esac
  shift
done

LOG_FILE="${LOG_DIR}/shenwan_v2_fetch_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "===== 申万二级行业/成分股抓取 ====="
echo "开始: $(date '+%F %T')"
echo "日志: ${LOG_FILE}"
echo "JSON: ${JSON}"

PY_ARGS=(--sectors-only --stable -o "${ROOT}")
if [[ "${FORCE}" -eq 1 ]]; then
  PY_ARGS+=(--force-refresh-sectors)
  echo "模式: 强制全量重抓"
else
  PY_ARGS+=(--refresh-stocks)
  echo "模式: 仅补缺失成分股（已有 stocks 的行业会跳过）"
fi
PY_ARGS+=("${EXTRA_ARGS[@]}")

count_missing() {
  "${PYTHON}" - "${JSON}" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
missing = sum(
    1 for s in data
    if not isinstance(s.get("stocks"), list) or len(s["stocks"]) == 0
)
print(missing)
PY
}

round=1
while [[ "${round}" -le "${MAX_ROUNDS}" ]]; do
  echo ""
  echo "--- 第 ${round}/${MAX_ROUNDS} 轮 ---"
  if "${PYTHON}" "${PY}" "${PY_ARGS[@]}"; then
    if [[ -f "${JSON}" ]]; then
      missing="$(count_missing)"
      echo "校验: 缺成分股的行业数 = ${missing}"
      if [[ "${missing}" -eq 0 ]]; then
        echo "完成: $(date '+%F %T')，${JSON} 已全部含 stocks"
        exit 0
      fi
      echo "仍有 ${missing} 个行业未抓到成分股，继续重试…"
    else
      echo "警告: 脚本成功但未找到 ${JSON}"
    fi
  else
    echo "本轮 Python 退出非 0"
  fi

  if [[ "${round}" -ge "${MAX_ROUNDS}" ]]; then
    break
  fi
  echo "等待 ${COOLDOWN_SEC}s 后进入下一轮（应对新浪限流）…"
  sleep "${COOLDOWN_SEC}"
  round=$((round + 1))
done

echo "失败: 已达最大轮次 ${MAX_ROUNDS}，请稍后重试或加大 --cooldown"
exit 1
