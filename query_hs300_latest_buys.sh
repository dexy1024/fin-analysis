#!/usr/bin/env bash
# 读取 logs/snapshots_hs300_YYYY.csv 中「最新时间戳」那一批记录，
# 筛出 实际交易动作=买入 的标的，每行输出：代码<TAB>名称
#
# 用法（在仓库根目录）:
#   ./query_hs300_latest_buys.sh
#   ./query_hs300_latest_buys.sh /path/to/snapshots_hs300_2026.csv

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="${1:-${ROOT}/logs/snapshots_hs300_2026.csv}"

if [[ ! -f "$CSV" ]]; then
  echo "文件不存在: $CSV" >&2
  exit 1
fi

exec python3 - "$CSV" <<'PY'
import csv
import sys
from pathlib import Path

path = Path(sys.argv[1])
best_ts = None
batch = []
with path.open(newline="", encoding="utf-8-sig") as f:
    r = csv.DictReader(f)
    for row in r:
        t = (row.get("时间") or "").strip()
        if not t:
            continue
        if best_ts is None or t > best_ts:
            best_ts = t
            batch = [row]
        elif t == best_ts:
            batch.append(row)

if not best_ts:
    sys.exit(0)

for row in batch:
    if (row.get("实际交易动作") or "").strip() != "买入":
        continue
    code = (row.get("代码") or "").strip()
    name = (row.get("名称") or "").strip()
    print(f"{code}\t{name}")
PY
