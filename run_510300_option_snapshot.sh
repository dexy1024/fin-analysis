#!/usr/bin/env bash
# 510300 期权平值/虚一档快照 + 开空条件看板
#
# 在仓库根目录执行:
#   ./run_510300_option_snapshot.sh           # 拉数并展示
#   ./run_510300_option_snapshot.sh --show    # 仅读已有 CSV 展示（不拉数）
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

if [[ -x "${ROOT}/.venv/bin/python3" ]]; then
  PYTHON="${ROOT}/.venv/bin/python3"
else
  PYTHON="python3"
fi

PY_FETCH="${ROOT}/backend/scripts/fetch_510300_option_snapshot.py"
CSV_OPTION="${ROOT}/10_510300_option_oi_iv.csv"
CSV_VOLUME="${ROOT}/11_510300_etf_volume.csv"

SHOW_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --show | --no-fetch)
      SHOW_ONLY=1
      ;;
    -h | --help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: ${arg}（可用 --show）" >&2
      exit 1
      ;;
  esac
done

if [[ "${SHOW_ONLY}" -eq 0 ]]; then
  echo ">> 拉取 510300 期权与现货数据..."
  "${PYTHON}" "${PY_FETCH}" -o "${ROOT}"
  echo
fi

if [[ ! -f "${CSV_OPTION}" ]]; then
  echo "未找到 ${CSV_OPTION}，请先执行本脚本（不加 --show）。" >&2
  exit 1
fi

"${PYTHON}" - "${CSV_OPTION}" "${CSV_VOLUME}" <<'PY'
import sys
from pathlib import Path

import pandas as pd

OPTION_CSV = Path(sys.argv[1])
VOLUME_CSV = Path(sys.argv[2])

df = pd.read_csv(OPTION_CSV)
if df.empty:
    print("期权 CSV 为空")
    sys.exit(1)

row0 = df.iloc[0]
trade_date = str(row0.get("数据日期", ""))
if len(trade_date) == 8 and trade_date.isdigit():
    trade_date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
else:
    trade_date_fmt = trade_date

close_px = row0.get("标的收盘价", row0.get("标的现价"))
ma30 = row0.get("MA30")
vol_ratio = row0.get("成交量比")
today_vol = row0.get("标的当日成交量")
avg5_vol = row0.get("标的前五日均成交量")

def fmt_num(v):
    if pd.isna(v):
        return "-"
    try:
        f = float(v)
        if f >= 1_000_000:
            return f"{f:,.0f}"
        if f == int(f):
            return str(int(f))
        return f"{f:.4g}"
    except (TypeError, ValueError):
        return str(v)

def cond_mark(ok: bool) -> str:
    return "✓" if ok else "✕"

def iv_pct(iv):
    if pd.isna(iv):
        return "-"
    v = float(iv)
    return f"{v * 100:.1f}%" if v <= 1.5 else f"{v:.1f}%"

below_ma30 = str(row0.get("收盘低于30日线", "")) == "是"
all_short = str(row0.get("开空满足", "")) == "是"

print("=" * 72)
print(f"  510300 期权开空看板 · 今日 ({trade_date_fmt}) 快照")
print("=" * 72)
print()
months = sorted(df["到期月份"].dropna().astype(str).unique().tolist()) if "到期月份" in df.columns else []
months_txt = ", ".join(months) if months else str(row0.get("到期月份", "-"))
print(f"  标的: {row0.get('标的名称', '510300')} ({row0.get('标的代码', '510300')})")
print(f"  到期月份: {months_txt}")
print(f"  收盘价: {close_px}    MA30: {ma30}    收盘<MA30: {cond_mark(below_ma30)}")
print(f"  当日成交量: {fmt_num(today_vol)}    前五日均量: {fmt_num(avg5_vol)}    成交量比: {vol_ratio}")
print()
print("  开空条件（四项须同时满足）")
print("  ─────────────────────────────────────────────────────────────────────")
print("    1. 收盘价 < MA30")
print("    2. 认沽隐含波动率 >= 25%")
print("    3. PCR > 1")
print("    4. 成交量比 > 1")
print()
print("  档位快照")
print("  ─────────────────────────────────────────────────────────────────────")
hdr = f"  {'档位':<12} {'到期月份':<8} {'收盘/MA30':<22} {'IV':<10} {'PCR':<8} {'成交量比':<10} {'开空满足'}"
print(hdr)
print("  " + "-" * 78)

for _, r in df.iterrows():
    label = str(r.get("档位", ""))
    strike = r.get("行权价")
    if not pd.isna(strike):
        if "平值" in label:
            label = f"平值 {strike:g}"
        elif "虚" in label:
            label = f"虚一档 {strike:g}"

    c_ok = str(r.get("收盘低于30日线", "")) == "是"
    if c_ok and not pd.isna(close_px) and not pd.isna(ma30):
        ma_txt = f"{close_px} < {ma30} {cond_mark(True)}"
    elif not pd.isna(close_px) and not pd.isna(ma30):
        ma_txt = f"{close_px} >= {ma30} {cond_mark(False)}"
    else:
        ma_txt = "同上" if _ != df.index[0] else "-"

    iv_val = r.get("认沽隐含波动率")
    iv_ok = str(r.get("波动率达标", "")) == "是"
    iv_txt = f"{iv_pct(iv_val)} {cond_mark(iv_ok)}"

    pcr_val = r.get("PCR")
    pcr_ok = str(r.get("PCR达标", "")) == "是"
    pcr_txt = f"{pcr_val:g} {cond_mark(pcr_ok)}" if not pd.isna(pcr_val) else f"- {cond_mark(False)}"

    vr_val = r.get("成交量比")
    vr_ok = str(r.get("成交量比达标", "")) == "是"
    vr_txt = f"{vr_val} {cond_mark(vr_ok)}" if not pd.isna(vr_val) else f"- {cond_mark(False)}"

    short_txt = str(r.get("开空满足", "否"))
    end_month = str(r.get("到期月份", "-"))
    print(f"  {label:<12} {end_month:<8} {ma_txt:<22} {iv_txt:<10} {pcr_txt:<8} {vr_txt:<10} {short_txt}")

print()
print("  持仓 / 合约明细")
print("  ─────────────────────────────────────────────────────────────────────")
for _, r in df.iterrows():
    label = str(r.get("档位", ""))
    strike = r.get("行权价")
    end_month = r.get("到期月份", "-")
    print(f"  [{end_month} · {label} {strike:g}]")
    print(f"    认沽: {r.get('认沽合约代码', '-')}  持仓 {fmt_num(r.get('认沽持仓量'))}  IV {iv_pct(r.get('认沽隐含波动率'))}")
    print(f"    认购: {r.get('认购合约代码', '-')}  持仓 {fmt_num(r.get('认购持仓量'))}  PCR {r.get('PCR', '-')}")
print()

any_short = (df["开空满足"] == "是").any() if "开空满足" in df.columns else False
if any_short:
    print("  >>> 综合结论: 有档位满足开空条件，可关注卖出认沽/卖权机会")
else:
    print("  >>> 综合结论: 当前无档位满足全部开空条件")

print()
print(f"  数据文件: {OPTION_CSV.name}", end="")
if VOLUME_CSV.is_file():
    print(f"  |  {VOLUME_CSV.name}")
else:
    print()
print("=" * 72)
PY
