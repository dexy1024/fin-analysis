#!/usr/bin/env bash
# 终止本机「作战指令快照」相关 Python；并列出谁打开了 snapshots_YYYY.csv。
# 两次快照之间常有长时间 sleep，瞬时 pgrep/lsof 为「无」是正常现象。
#
# 用法（项目根目录）：
#   ./stop_trade_command_processes.sh              # 立即查一次并尝试 kill
#   ./stop_trade_command_processes.sh --dry-run    # 只诊断不 kill
#   ./stop_trade_command_processes.sh --watch 300  # 最长 300 秒内每 2 秒采样，命中即 kill 并退出
#
# 若 watch 仍抓不到但 CSV 仍长：请到当初开循环的终端 jobs -l / Ctrl+C（杀的是 zsh 里的 while，不是 Python）。

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
YEAR="$(date +%Y)"
CSV="${ROOT}/logs/snapshots_${YEAR}.csv"
AUDIT="${ROOT}/logs/snapshot_engine_runs.log"
DRY=0
WATCH_SECS=""

while (($#)); do
  case "$1" in
    --dry-run | -n)
      DRY=1
      shift
      ;;
    --watch)
      WATCH_SECS="${2:-}"
      if ! [[ "${WATCH_SECS}" =~ ^[0-9]+$ ]]; then
        echo "用法: $0 --watch <秒数>  例如: $0 --watch 300" >&2
        exit 2
      fi
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

_print_audit_hint() {
  echo "=== 0) 最近审计 logs/snapshot_engine_runs.log（比 pgrep 可靠；每轮引擎启动即记一行）==="
  if [[ -f "${AUDIT}" ]]; then
    tail -3 "${AUDIT}" 2>/dev/null || true
    echo ""
    echo "若 parent_command 为 /bin/zsh -il：到该 zsh 终端 jobs -l / Ctrl+C 停循环；"
    echo "ppid 可用: ps -o pid,ppid,etime,command -p <ppid>"
  else
    echo "(无审计文件)"
  fi
  echo ""
}

_run_diag() {
  _print_audit_hint
  echo "=== 1) pgrep run_trade_command ==="
  pgrep -fl "run_trade_command" 2>/dev/null || echo "(无)"
  echo ""
  echo "=== 2) ps 中含 run_trade_command / generate_snapshots ==="
  ps aux 2>/dev/null | grep -E '[r]un_trade_command\.py|[g]enerate_snapshots\.sh' || echo "(无)"
  echo ""
  echo "=== 3) lsof ${CSV} ==="
  if [[ -f "${CSV}" ]]; then
    lsof "${CSV}" 2>/dev/null || echo "(无：当前无进程打开该文件；快照写入只有几秒窗口)"
  else
    echo "(文件不存在)"
  fi
}

_do_kill() {
  if [[ "${DRY}" -eq 1 ]]; then
    echo "[--dry-run] 跳过 kill。"
    return 0
  fi
  echo ""
  echo "=== 尝试 SIGTERM / SIGKILL（run_trade_command.py）==="
  _kp() { pgrep -f "$1" >/dev/null 2>&1 && echo "TERM $1" && pkill -TERM -f "$1" 2>/dev/null || true; }
  _kp "${ROOT}/backend/run_trade_command.py"
  _kp "[/]backend/run_trade_command.py"
  _kp "run_trade_command.py"
  sleep 1
  for pat in "${ROOT}/backend/run_trade_command.py" "[/]backend/run_trade_command.py" "run_trade_command.py"; do
    if pgrep -f "${pat}" >/dev/null 2>&1; then
      echo "KILL ${pat}"
      pkill -KILL -f "${pat}" 2>/dev/null || true
    fi
  done
}

if [[ -n "${WATCH_SECS}" ]]; then
  _print_audit_hint
  echo "监视 ${WATCH_SECS} 秒（每 2 秒采样）；命中 run_trade_command 或 lsof 到 CSV 则 kill（非 dry-run）并退出。"
  echo "（下一轮快照若在 5 分钟后，请把秒数设大一点，例如 --watch 600）"
  echo ""
  start_ts=$(date +%s)
  while (( $(date +%s) - start_ts < WATCH_SECS )); do
    tick=$(date +%H:%M:%S)
    hit=0
    if pgrep -f "run_trade_command" >/dev/null 2>&1; then
      echo ""
      echo ">>> [${tick}] pgrep 命中 <<<"
      pgrep -fl "run_trade_command" 2>/dev/null || true
      hit=1
    fi
    if [[ -f "${CSV}" ]]; then
      lo=$(lsof "${CSV}" 2>/dev/null || true)
      if [[ -n "${lo}" ]]; then
        echo ""
        echo ">>> [${tick}] lsof 命中 <<<"
        echo "${lo}"
        hit=1
      fi
    fi
    if [[ "${hit}" -eq 1 ]]; then
      _do_kill
      echo ""
      echo "已处理命中项，退出监视。"
      exit 0
    fi
    sleep 2
  done
  echo ""
  echo "监视结束：窗口内未捕获到快照进程（可能本轮未触发，或间隔大于 ${WATCH_SECS}s）。"
  exit 0
fi

_run_diag

if [[ "${DRY}" -eq 1 ]]; then
  echo ""
  echo "[--dry-run] 未发送 kill。"
  exit 0
fi

_do_kill

echo ""
echo "=== 再次 pgrep ==="
pgrep -fl "run_trade_command" 2>/dev/null || echo "(无)"

echo ""
echo "说明：若上面全是「无」，多半当前正处在 sleep；请用 --watch 600 包住下一轮，"
echo "或到父 zsh 终端执行 jobs -l 停掉 while 循环。"
