#!/usr/bin/env bash
# 读取 logs/snapshots_hs300_YYYY.csv 中「最新时间戳」那一批记录，
# 筛出 实际交易动作=买入 的标的：
#   - stdout：每行 代码<TAB>名称（该批全部买入信号）
#   - 默认将其中尚未出现在 backend/data/observation.json 的标的追加到 observations
#
# 用法（在仓库根目录）:
#   ./query_hs300_latest_buys.sh
#   ./query_hs300_latest_buys.sh /path/to/snapshots_hs300_2026.csv
#   ./query_hs300_latest_buys.sh --dry-run
#   ./query_hs300_latest_buys.sh --dry-run /path/to/snapshots_hs300_2026.csv

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

CSV="${1:-${ROOT}/logs/snapshots_hs300_2026.csv}"
OBS_JSON="${ROOT}/backend/data/observation.json"

if [[ ! -f "$CSV" ]]; then
  echo "文件不存在: $CSV" >&2
  exit 1
fi
if [[ ! -f "$OBS_JSON" ]]; then
  echo "文件不存在: $OBS_JSON" >&2
  exit 1
fi

exec python3 - "$CSV" "$OBS_JSON" "$DRY_RUN" <<'PY'
import csv
import json
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
obs_path = Path(sys.argv[2])
dry_run = sys.argv[3] == "1"

best_ts = None
batch = []
with csv_path.open(newline="", encoding="utf-8-sig") as f:
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

buys: list[tuple[str, str]] = []
for row in batch:
    if (row.get("实际交易动作") or "").strip() != "买入":
        continue
    code = (row.get("代码") or "").strip()
    name = (row.get("名称") or "").strip()
    if not code:
        continue
    buys.append((code, name))

for code, name in buys:
    print(f"{code}\t{name}")

if not buys:
    print("本批无「实际交易动作=买入」标的，跳过 observation.json。", file=sys.stderr)
    sys.exit(0)

text = obs_path.read_text(encoding="utf-8")
data = json.loads(text)
obs = data.get("observations")
if not isinstance(obs, list):
    print("observation.json 缺少 observations 数组", file=sys.stderr)
    sys.exit(1)

existing = {str(o.get("code", "")).strip() for o in obs if isinstance(o, dict)}
added = 0
skipped = 0
for code, name in buys:
    if code in existing:
        skipped += 1
        continue
    obs.append({"code": code, "name": name})
    existing.add(code)
    added += 1

if dry_run:
    print(
        f"[dry-run] 最新时间={best_ts!r} 本批买入={len(buys)} "
        f"将追加={added} 已存在跳过={skipped}（未写文件）",
        file=sys.stderr,
    )
    sys.exit(0)

if added == 0:
    print(
        f"最新时间={best_ts!r} 本批买入={len(buys)}，"
        f"observation.json 均已存在，未追加。",
        file=sys.stderr,
    )
    sys.exit(0)

out_obj = {
    "_comment": data.get(
        "_comment",
        "观察标的维护文件。直接编辑此文件添加/删除/修改，无需重启服务。",
    ),
    "observations": obs,
}
obs_path.write_text(
    json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(
    f"最新时间={best_ts!r} 已追加 {added} 条到 {obs_path}（跳过已存在 {skipped} 条）。",
    file=sys.stderr,
)
PY
