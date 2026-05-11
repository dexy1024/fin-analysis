#!/usr/bin/env bash
# 终止本机所有「作战指令快照」Python 进程（run_trade_command.py），不关你的 zsh 登录 shell。
# 若快照仍周期性出现，说明某终端里还有 while/sleep 循环在反复 exec，请到该终端 jobs/fg 或 Ctrl+C。
#
# 用法：在项目根目录执行
#   chmod +x stop_trade_command_processes.sh && ./stop_trade_command_processes.sh

set -euo pipefail

echo "=== 当前匹配的进程（run_trade_command.py）==="
pgrep -fl "run_trade_command\.py" 2>/dev/null || echo "(无)"

if pgrep -f "run_trade_command\.py" >/dev/null 2>&1; then
  pkill -TERM -f "run_trade_command\.py" 2>/dev/null || true
  sleep 1
  if pgrep -f "run_trade_command\.py" >/dev/null 2>&1; then
    echo "仍有残留，发送 SIGKILL..."
    pkill -KILL -f "run_trade_command\.py" 2>/dev/null || true
  fi
  echo "已尝试结束所有 run_trade_command.py。"
else
  echo "未发现 run_trade_command.py 进程（可能已退出，或循环尚未触发本轮）。"
fi

echo ""
echo "=== 再次检查 ==="
pgrep -fl "run_trade_command\.py" 2>/dev/null || echo "(无)"

echo ""
echo "若 CSV 仍定时增长：请到「cwd 为 fin-analysis 且曾跑过快照」的终端执行:"
echo "  jobs -l"
echo "  # 若有 while/sleep 或 bash 子任务，用 kill %<编号> 或在该终端按 Ctrl+C"
