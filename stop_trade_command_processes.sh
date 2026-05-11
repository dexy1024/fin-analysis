#!/usr/bin/env bash
# 终止本机「作战指令快照」相关 Python；并列出谁打开了 snapshots_YYYY.csv（便于发现 sleep 间隙里 pgrep 为空的假象）。
# 不杀 zsh 登录 shell（例如 ppid 62047 的 /bin/zsh -il）；若循环在 shell 里，请到该终端 jobs / Ctrl+C。
#
# 用法：在项目根目录
#   ./stop_trade_command_processes.sh
# 仅诊断不杀进程：
#   ./stop_trade_command_processes.sh --dry-run

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
YEAR="$(date +%Y)"
CSV="${ROOT}/logs/snapshots_${YEAR}.csv"
DRY=0
if [[ "${1:-}" == "--dry-run" ]] || [[ "${1:-}" == "-n" ]]; then
  DRY=1
fi

echo "=== 1) pgrep（run_trade_command；两次运行之间的 sleep 段常为无）==="
pgrep -fl "run_trade_command" 2>/dev/null || echo "(无)"

echo ""
echo "=== 2) ps 中含 run_trade_command.py / generate_snapshots（排除 grep 自身）==="
ps aux 2>/dev/null | grep -E '[r]un_trade_command\.py|[g]enerate_snapshots\.sh' || echo "(无)"

echo ""
echo "=== 3) 谁打开了 ${CSV}（写入方或 Excel 等；快照运行时常能看到 Python）==="
if [[ -f "${CSV}" ]]; then
  lsof "${CSV}" 2>/dev/null || echo "(lsof 无输出：可能无人占用，或权限不足)"
else
  echo "(文件不存在: ${CSV})"
fi

if [[ "${DRY}" -eq 1 ]]; then
  echo ""
  echo "[--dry-run] 未发送任何 kill。"
  exit 0
fi

echo ""
echo "=== 4) 尝试结束快照 Python（按命令行匹配，尽量不误伤）==="

_kill_pattern() {
  local pat="$1"
  if pgrep -f "${pat}" >/dev/null 2>&1; then
    echo "发送 SIGTERM: ${pat}"
    pkill -TERM -f "${pat}" 2>/dev/null || true
  fi
}

# 绝对路径（从项目根跑、从别处带路径跑都能命中）
_kill_pattern "${ROOT}/backend/run_trade_command.py"
# 相对路径 argv 常见形式
_kill_pattern "[/]backend/run_trade_command.py"
_kill_pattern "run_trade_command.py"

sleep 1

for pat in "${ROOT}/backend/run_trade_command.py" "[/]backend/run_trade_command.py" "run_trade_command.py"; do
  if pgrep -f "${pat}" >/dev/null 2>&1; then
    echo "仍有匹配「${pat}」，SIGKILL..."
    pkill -KILL -f "${pat}" 2>/dev/null || true
  fi
done

echo ""
echo "=== 5) 再次 pgrep run_trade_command ==="
pgrep -fl "run_trade_command" 2>/dev/null || echo "(无)"

echo ""
echo "若 3) 里曾有 Python 但 4) 杀完 CSV 仍定时增长：多半是「父 zsh + while/sleep」在拉起下一轮，"
echo "请到对应终端执行: jobs -l  然后 kill %<job> 或 Ctrl+C。"
