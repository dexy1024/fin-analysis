#!/usr/bin/env python3
"""
梅花2test（889999）：从 600873 复制基座，并追加「日历上在未来」的日线 + 60m K 线。

889999 在 `get_index_kline` 中会按本地 CSV 最大时间放宽 end_ts（无需再设 MEIHUA2TEST_FUTURE_K）；
若需按「当前时刻」截断 mock 文件，可设 `MEIHUA2TEST_FUTURE_K=0`。

用法（在 backend 目录）:

  python3 scripts/build_meihua2test_fixture.py

输出：
  - tests/fixtures/meihua2test/a_daily_qfq_889999.csv、kline_60_889999.csv
  - backend/data 下同名文件

脚本结束时会试算 `analyze_meihua2test_symbol`：full_trigger 为假时仅打印提示，不失败退出。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIX = ROOT / "tests" / "fixtures" / "meihua2test"

SRC_CODE = "600873"
MEI_CODE = "889999"

# A 股 60m 常见槽位（与东财/新浪序列一致）
_SLOTS: list[tuple[int, int]] = [(10, 30), (11, 30), (14, 0), (15, 0)]


def _next_weekday(dt: pd.Timestamp) -> pd.Timestamp:
    t = dt + pd.Timedelta(days=1)
    while t.weekday() >= 5:
        t += pd.Timedelta(days=1)
    return t


def _trading_datetimes_after(last_60m_ts: pd.Timestamp, n_slots: int) -> list[pd.Timestamp]:
    """从「源最后一根 60m 的下一交易日起」顺排 n 个 60m 节点。"""
    d = last_60m_ts.normalize() + pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    out: list[pd.Timestamp] = []
    while len(out) < n_slots:
        while d.weekday() >= 5:
            d += pd.Timedelta(days=1)
        for hour, minute in _SLOTS:
            if len(out) >= n_slots:
                break
            out.append(d.replace(hour=hour, minute=minute, second=0, microsecond=0))
        d = _next_weekday(d.replace(hour=0, minute=0, second=0, microsecond=0))
    return out


def _append_future_60m(h: pd.DataFrame, n_tail: int) -> pd.DataFrame:
    h = h.sort_values("date").reset_index(drop=True)
    last_ts = pd.to_datetime(h["date"].iloc[-1])
    n = min(n_tail, len(h))
    template = h.tail(n).reset_index(drop=True)
    new_dates = _trading_datetimes_after(last_ts, n)
    for i in range(n):
        template.loc[i, "date"] = new_dates[i]
    merged = pd.concat([h, template], ignore_index=True)
    return merged.sort_values("date").reset_index(drop=True)


def _append_future_daily(dd: pd.DataFrame, h_extended: pd.DataFrame) -> pd.DataFrame:
    dd = dd.copy()
    dd["date"] = pd.to_datetime(dd["date"])
    last_d = dd["date"].max()
    h2 = h_extended.copy()
    h2["date"] = pd.to_datetime(h2["date"])
    extra_days = sorted({ts.normalize() for ts in h2["date"] if ts.normalize() > last_d})
    rows: list[dict[str, object]] = []
    for day in extra_days:
        if day.weekday() >= 5:
            continue
        day_df = h2[h2["date"].dt.normalize() == day]
        if day_df.empty:
            continue
        o = float(day_df.iloc[0]["open"])
        hi = float(day_df["high"].max())
        lo = float(day_df["low"].min())
        c = float(day_df.iloc[-1]["close"])
        vol = float(day_df["volume"].sum())
        rows.append({"date": day, "open": o, "high": hi, "low": lo, "close": c, "volume": vol})
    if not rows:
        return dd.sort_values("date").reset_index(drop=True)
    extra = pd.DataFrame(rows)
    return pd.concat([dd, extra], ignore_index=True).sort_values("date").reset_index(drop=True)


def _verify_radar_row() -> None:
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from services.defense_radar import analyze_meihua2test_symbol

    row = analyze_meihua2test_symbol(refresh=False)
    if row.full_trigger:
        print("校验: full_trigger=True | last_price=", row.last_price)
    else:
        print(
            "提示: full_trigger=False（mock 末段若未满足四条件扳机属正常，889999 仍可按 CSV 出图）",
            "| last_price=",
            row.last_price,
            file=sys.stderr,
        )


def main() -> None:
    src_daily = DATA / f"a_daily_qfq_{SRC_CODE}.csv"
    src_60 = DATA / f"kline_60_{SRC_CODE}.csv"
    if not src_daily.is_file() or not src_60.is_file():
        print("缺少 600873 源文件:", src_daily, src_60, file=sys.stderr)
        sys.exit(1)

    FIX.mkdir(parents=True, exist_ok=True)

    h = pd.read_csv(src_60, parse_dates=["date"])
    dd = pd.read_csv(src_daily, parse_dates=["date"])

    n_tail = 16
    h_out = _append_future_60m(h, n_tail)
    dd_out = _append_future_daily(dd, h_out)

    dd_out["date"] = dd_out["date"].dt.strftime("%Y-%m-%d")
    h_out["date"] = h_out["date"].dt.strftime("%Y-%m-%d %H:%M:%S")

    out_fix_d = FIX / f"a_daily_qfq_{MEI_CODE}.csv"
    out_fix_h = FIX / f"kline_60_{MEI_CODE}.csv"
    out_data_d = DATA / f"a_daily_qfq_{MEI_CODE}.csv"
    out_data_h = DATA / f"kline_60_{MEI_CODE}.csv"

    dd_out.to_csv(out_fix_d, index=False)
    h_out.to_csv(out_fix_h, index=False)
    shutil.copy2(out_fix_d, out_data_d)
    shutil.copy2(out_fix_h, out_data_h)

    print("fixtures:", out_fix_d)
    print("fixtures:", out_fix_h)
    print("installed:", out_data_d)
    print("installed:", out_data_h)
    print("last 60m row:", h_out["date"].iloc[-1])

    _verify_radar_row()


if __name__ == "__main__":
    main()
