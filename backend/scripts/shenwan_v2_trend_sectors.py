#!/usr/bin/env python3
"""
申万二级 · 趋势行业综合视图：合并 analysis / volume_price / crowding 三份快照。

决策流程（四步）：
  1. 容量门槛：成交额占比 ≥ MIN_CAPACITY_SHARE_PCT（默认 1%）一票否决
  2. 分层：核心 / 主线 / 量价 / 早期 / 观察
  3. 拥挤边际：分位 ≥ 95% 时看占比 3 日变化，区分抱团稳固 vs 资金撤离
  4. B 组复活：主线缺量价三重 + 占比 ≥ 3%，回踩 MA20 缩量 → 复活观察
  5. 持筹信号 + 出局阈值 + 趋势标记（方向）+ 可做标记（今日可参与）

用法：
    python3 backend/scripts/shenwan_v2_trend_sectors.py
    python3 backend/scripts/shenwan_v2_trend_sectors.py -o . --min-capacity 0.5
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from shenwan_v2_sector_analysis import (  # noqa: E402
    RESULT_CSV as ANALYSIS_CSV,
    _sector_code,
    _sector_name,
    build_sw_index_code_map,
    load_sectors_json,
    resolve_sw_index_code,
)
from shenwan_v2_crowding_monitor import RESULT_CSV as CROWDING_CSV  # noqa: E402
from shenwan_v2_volume_price_signals import (  # noqa: E402
    RESULT_CSV as VOLUME_CSV,
    fetch_sector_ohlc_amount,
)

RESULT_CSV = "shenwan_v2_trend_sectors.csv"
ACTIONABLE_CSV = "shenwan_v2_actionable_sectors.csv"

TIER_CORE = "核心"
TIER_MAIN = "主线"
TIER_VOLUME = "量价"
TIER_EARLY = "早期"
TIER_WATCH = "观察"
TIER_REVIVAL = "复活"
TIER_ORDER = {
    TIER_CORE: 0,
    TIER_REVIVAL: 1,
    TIER_MAIN: 2,
    TIER_VOLUME: 3,
    TIER_EARLY: 4,
    TIER_WATCH: 5,
}

STATUS_CROWDED = "拥挤"
STATUS_EARLY = "异动"
WATCH_PCT_MIN = 70.0

# --- 四维优化参数 ---
MIN_CAPACITY_SHARE_PCT = 1.0
SUPER_MAIN_SHARE_PCT = 3.0
CROWDING_PCT_HIGH = 95.0
SHARE_DELTA_EXIT = -2.0
SHARE_DELTA_WARN = -0.5
MA20_WINDOW = 20
MA20_PROXIMITY_PCT = 3.0
VOL_SHRINK_RATIO = 0.85
VOL_AVG_WINDOW = 5

SIGNAL_HOLD = "持有"
SIGNAL_SOLID = "抱团稳固"
SIGNAL_WARN = "警惕撤离"
SIGNAL_EXIT = "建议出局"
SIGNAL_REVIVAL = "复活观察"
SIGNAL_WATCH = "观察"
HOLD_SIGNAL_BLOCK = frozenset({SIGNAL_WARN, SIGNAL_EXIT})

# --- 可做 vs 延伸 ---
MODE_EXTENDED = "延伸观察"
MODE_ACTIONABLE = "可做"
MODE_ROTATION = "轮动试探"

ACTIONABLE_RET20_MAX = 22.0
ACTIONABLE_PCT120_MAX = 90.0
ACTIONABLE_PCT120_SWEET_LO = 20.0
ACTIONABLE_PCT120_SWEET_HI = 75.0
ACTIONABLE_RET20_SWEET_LO = 3.0
ACTIONABLE_RET20_SWEET_HI = 15.0
# 今日可同时标记的「可做」行业数（按可做得分 Top N，仅得分>0 且模式为可做/轮动）
MAX_ACTIONABLE_SECTORS = 3

RESULT_COLUMNS = (
    "数据日期",
    "参与模式",
    "趋势层级",
    "趋势标记",
    "可做标记",
    "持筹信号",
    "行业代码",
    "行业名称",
    "近20日涨幅",
    "超额收益率",
    "成交额占比_pct",
    "占比120日分位数",
    "占比3日变化_pct",
    "分位数3日斜率",
    "拥挤度状态",
    "连续3日放量",
    "量价三重",
    "成交额暴增倍数",
    "说明",
    "出局阈值",
)

ACTIONABLE_COLUMNS = (
    "数据日期",
    "可做标记",
    "参与模式",
    "行业代码",
    "行业名称",
    "近20日涨幅",
    "超额收益率",
    "成交额占比_pct",
    "占比120日分位数",
    "占比3日变化_pct",
    "分位数3日斜率",
    "连续3日放量",
    "可做得分",
    "说明",
)


def _load_csv(path: Path, label: str) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {label}：{path}")
    return pd.read_csv(path)


def _passes_capacity(share_pct: float | None, min_capacity: float) -> bool:
    if share_pct is None or pd.isna(share_pct):
        return False
    return float(share_pct) >= min_capacity


def _classify_tier(
    *,
    in_analysis: bool,
    has_volume: bool,
    crowd_status: str | None,
    pct_120: float | None,
    passes_capacity: bool,
    revival: bool,
) -> str | None:
    if revival:
        return TIER_REVIVAL

    crowded = crowd_status == STATUS_CROWDED
    early = crowd_status == STATUS_EARLY

    if not passes_capacity:
        if early:
            return TIER_EARLY
        return None

    if in_analysis and has_volume and crowded:
        return TIER_CORE
    if in_analysis and crowded:
        return TIER_MAIN
    if in_analysis and has_volume:
        return TIER_VOLUME
    if early:
        return TIER_EARLY
    if in_analysis and pct_120 is not None and pct_120 >= WATCH_PCT_MIN and not crowded and not early:
        return TIER_WATCH
    return None


def _crowding_marginal_signal(
    pct_120: float | None,
    share_delta_3d: float | None,
    pct_slope_3d: float | None,
) -> str:
    """拥挤度 > 95% 时，用占比边际变化区分抱团 vs 撤离。"""
    if pct_120 is None or pct_120 < CROWDING_PCT_HIGH:
        return SIGNAL_HOLD

    if share_delta_3d is not None and share_delta_3d <= SHARE_DELTA_EXIT:
        return SIGNAL_EXIT

    if share_delta_3d is not None and share_delta_3d <= SHARE_DELTA_WARN:
        return SIGNAL_WARN
    if pct_slope_3d is not None and pct_slope_3d <= SHARE_DELTA_WARN:
        return SIGNAL_WARN

    return SIGNAL_SOLID


def _exit_threshold_text(share_pct: float | None, pct_120: float | None) -> str:
    base = f"占比3日降>{abs(SHARE_DELTA_EXIT):.1f}pct→出局"
    if pct_120 is not None and pct_120 >= CROWDING_PCT_HIGH:
        sp = f"{share_pct:.2f}" if share_pct is not None else "?"
        return (
            f"{base}；分位≥{CROWDING_PCT_HIGH:.0f}%时占比3日斜率<{SHARE_DELTA_WARN}→警惕；"
            f"当前占比{sp}%"
        )
    return base


def _check_b_revival(df: pd.DataFrame) -> bool:
    """
    B 组复活：回踩 MA20（±3%）+ 缩量（当日成交额 < 5 日均 × 0.85）+ MA20 仍向上。
    """
    if len(df) < MA20_WINDOW + 2:
        return False
    close = df["close"]
    amount = df["amount"]
    ma20 = close.rolling(MA20_WINDOW).mean()
    ma20_now = float(ma20.iloc[-1])
    ma20_prev = float(ma20.iloc[-2])
    if pd.isna(ma20_now) or pd.isna(ma20_prev) or ma20_now <= ma20_prev:
        return False

    price = float(close.iloc[-1])
    dist_pct = abs(price - ma20_now) / ma20_now * 100.0
    if dist_pct > MA20_PROXIMITY_PCT:
        return False

    avg5 = float(amount.iloc[-VOL_AVG_WINDOW:].mean())
    today_amt = float(amount.iloc[-1])
    if avg5 <= 0 or today_amt >= avg5 * VOL_SHRINK_RATIO:
        return False

    return True


def _fetch_revival_flags(
    codes: list[str],
    sector_by_code: dict[str, dict[str, Any]],
    code_map: dict[str, str],
) -> dict[str, bool]:
    out: dict[str, bool] = {c: False for c in codes}
    for code in codes:
        sector = sector_by_code.get(code)
        if not sector:
            continue
        name = _sector_name(sector)
        sw = resolve_sw_index_code(name, code_map)
        if not sw:
            continue
        try:
            df = fetch_sector_ohlc_amount(sw)
            out[code] = _check_b_revival(df)
        except Exception:
            logging.exception("复活检测失败 %s", code)
        time.sleep(0.15)
    return out


def _tier_note(
    tier: str,
    vol_up: bool,
    hold_signal: str,
    revival: bool,
) -> str:
    if revival or tier == TIER_REVIVAL:
        return "主线超级赛道回踩MA20缩量，复活观察位"
    if tier == TIER_CORE:
        extra = "；连3日放量" if vol_up else ""
        if hold_signal == SIGNAL_SOLID:
            return f"价格+资金+拥挤三重共振{extra}，100%分位但占比未衰减"
        if hold_signal == SIGNAL_WARN:
            return f"三重共振{extra}，占比边际下滑需警惕"
        if hold_signal == SIGNAL_EXIT:
            return f"三重共振但占比断崖式撤离，建议降仓"
        return f"价格+资金+拥挤三重共振{extra}，主线最强"
    if tier == TIER_MAIN:
        extra = "，连3日放量" if vol_up else ""
        return f"趋势+资金拥挤{extra}，缺量价三重或待复活"
    if tier == TIER_VOLUME:
        return "趋势+120日新高放量，资金占比尚未极端拥挤"
    if tier == TIER_EARLY:
        return "冷门区放量突破，价格趋势未确认，偏左侧观察"
    if tier == TIER_WATCH:
        return "趋势成立，资金占比偏高但未标拥挤"
    return ""


def _pick_trend_mark(df: pd.DataFrame) -> pd.Series:
    """趋势标记：最强趋势方向（含延伸段，供对照）。"""
    marks = pd.Series(0, index=df.index, dtype=int)
    if df.empty:
        return marks

    eligible = df[df["趋势层级"] == TIER_CORE].copy()
    if eligible.empty:
        eligible = df[df["趋势层级"] == TIER_REVIVAL].copy()
    if eligible.empty:
        eligible = df[df["趋势层级"] == TIER_MAIN].copy()
    if eligible.empty:
        return marks

    eligible = eligible.copy()
    eligible["_share_delta"] = pd.to_numeric(eligible["占比3日变化_pct"], errors="coerce").fillna(-999)
    eligible["_ret"] = pd.to_numeric(eligible["近20日涨幅"], errors="coerce").fillna(-1)
    eligible["_vol3"] = pd.to_numeric(eligible["连续3日放量"], errors="coerce").fillna(0)
    eligible["_v3"] = pd.to_numeric(eligible["量价三重"], errors="coerce").fillna(0)
    eligible = eligible[eligible["持筹信号"] != SIGNAL_EXIT]
    if eligible.empty:
        return marks

    best = eligible.sort_values(
        ["_v3", "_vol3", "_share_delta", "_ret"],
        ascending=[False, False, False, False],
    ).index[0]
    marks.loc[best] = 1
    return marks


def _classify_participation(
    *,
    in_analysis: bool,
    tier: str | None,
    ret20: float | None,
    pct_120: float | None,
    hold_signal: str,
    crowd_status: str | None,
    share_delta: float | None,
    pct_slope: float | None,
    passes_capacity: bool,
) -> str:
    """延伸观察 / 可做 / 轮动试探。"""
    ret = float(ret20) if ret20 is not None and pd.notna(ret20) else None
    pct = float(pct_120) if pct_120 is not None and pd.notna(pct_120) else None

    if crowd_status == STATUS_EARLY and passes_capacity:
        if (share_delta or 0) > 0 or (pct_slope or 0) > 5:
            return MODE_ROTATION
        return MODE_ROTATION

    if ret is not None and ret > ACTIONABLE_RET20_MAX:
        return MODE_EXTENDED
    if pct is not None and pct >= 98.0:
        return MODE_EXTENDED
    if hold_signal in HOLD_SIGNAL_BLOCK:
        return MODE_EXTENDED

    if in_analysis and passes_capacity and ret is not None and pct is not None:
        if ret <= ACTIONABLE_RET20_MAX and pct < ACTIONABLE_PCT120_MAX:
            return MODE_ACTIONABLE

    if (
        passes_capacity
        and pct is not None
        and ACTIONABLE_PCT120_SWEET_LO <= pct <= ACTIONABLE_PCT120_SWEET_HI
        and (share_delta or 0) > 0
        and (pct_slope or 0) > 0
    ):
        return MODE_ROTATION

    if tier in (TIER_WATCH, TIER_VOLUME) and passes_capacity:
        return MODE_ACTIONABLE

    return MODE_EXTENDED


def _actionable_score(row: dict[str, Any]) -> float:
    """越高越「趋势在 + 资金轮动来 + 还能接」。"""
    ret = float(row.get("近20日涨幅") or 0)
    pct = float(row.get("占比120日分位数") or 0)
    sd = float(row.get("占比3日变化_pct") or 0)
    ps = float(row.get("分位数3日斜率") or 0)
    vol_up = int(row.get("连续3日放量") or 0)
    mode = row.get("参与模式", "")
    hold = row.get("持筹信号", "")

    if mode not in (MODE_ACTIONABLE, MODE_ROTATION):
        return -1.0

    # 硬门槛一票否决：高位/拥挤/撤离信号直接锁死为延伸观察
    if ret > ACTIONABLE_RET20_MAX:
        return -1.0
    if pct > ACTIONABLE_PCT120_MAX:
        return -1.0
    if hold in HOLD_SIGNAL_BLOCK:
        return -1.0

    score = 0.0
    if ACTIONABLE_RET20_SWEET_LO <= ret <= ACTIONABLE_RET20_SWEET_HI:
        score += 50.0
    elif ret > ACTIONABLE_RET20_SWEET_HI:
        score += max(0.0, 50.0 - (ret - ACTIONABLE_RET20_SWEET_HI) * 10.0)
    else:
        score += max(0.0, 30.0 - abs(ret - ACTIONABLE_RET20_SWEET_LO) * 5.0)
    if ACTIONABLE_PCT120_SWEET_LO <= pct <= ACTIONABLE_PCT120_SWEET_HI:
        score += 30.0
    if sd > 0:
        score += min(sd * 25.0, 35.0)
    if ps > 0:
        score += min(ps * 2.5, 25.0)
    if vol_up:
        score += 6.0
    if mode == MODE_ACTIONABLE:
        score += 8.0
    return score


def _build_actionable_universe(
    analysis: pd.DataFrame,
    crowding: pd.DataFrame,
    data_date: str,
    *,
    min_capacity: float,
    max_actionable: int = MAX_ACTIONABLE_SECTORS,
) -> pd.DataFrame:
    """从 analysis 全量 × crowding 扫可做，不依赖 trend 分层是否收录。"""
    crowd_by = crowding.set_index("行业代码").to_dict("index")
    rows: list[dict[str, Any]] = []

    for _, a in analysis.iterrows():
        code = str(a["行业代码"])
        c = crowd_by.get(code)
        if not c:
            continue
        share_pct = float(c["成交额占比_pct"]) if pd.notna(c.get("成交额占比_pct")) else None
        if not _passes_capacity(share_pct, min_capacity):
            continue

        ret20 = float(a["近20日涨幅"]) if pd.notna(a.get("近20日涨幅")) else None
        pct_120 = float(c["占比120日分位数"]) if pd.notna(c.get("占比120日分位数")) else None
        share_delta = (
            float(c["占比3日变化_pct"]) if pd.notna(c.get("占比3日变化_pct")) else None
        )
        pct_slope = float(c["分位数3日斜率"]) if pd.notna(c.get("分位数3日斜率")) else None
        vol_up = int(c.get("连续3日放量", 0) or 0)
        crowd_status = c.get("拥挤度状态")
        hold = _crowding_marginal_signal(pct_120, share_delta, pct_slope)

        mode = _classify_participation(
            in_analysis=True,
            tier=TIER_WATCH,
            ret20=ret20,
            pct_120=pct_120,
            hold_signal=hold,
            crowd_status=crowd_status,
            share_delta=share_delta,
            pct_slope=pct_slope,
            passes_capacity=True,
        )

        item = {
            "数据日期": data_date,
            "可做标记": 0,
            "参与模式": mode,
            "行业代码": code,
            "行业名称": a.get("行业名称", code),
            "近20日涨幅": ret20,
            "超额收益率": a.get("超额收益率"),
            "成交额占比_pct": share_pct,
            "占比120日分位数": pct_120,
            "占比3日变化_pct": share_delta,
            "分位数3日斜率": pct_slope,
            "连续3日放量": vol_up,
            "持筹信号": hold,
        }
        item["可做得分"] = round(_actionable_score(item), 2)
        if mode == MODE_ACTIONABLE:
            item["说明"] = "趋势已确认，拥挤未极致，资金仍有上行空间"
        elif mode == MODE_ROTATION:
            item["说明"] = "资金占比抬升中，趋势待确认或早期轮动"
        else:
            item["说明"] = "趋势强但延伸段，仅观察"
        rows.append(item)

    # 轮动：crowding 异动且容量够、不在 analysis
    analysis_codes = set(analysis["行业代码"].astype(str))
    for code, c in crowd_by.items():
        if code in analysis_codes:
            continue
        if c.get("拥挤度状态") != STATUS_EARLY:
            continue
        share_pct = float(c.get("成交额占比_pct", 0) or 0)
        if not _passes_capacity(share_pct, min_capacity):
            continue
        pct_120 = float(c["占比120日分位数"]) if pd.notna(c.get("占比120日分位数")) else None
        share_delta = float(c["占比3日变化_pct"]) if pd.notna(c.get("占比3日变化_pct")) else None
        pct_slope = float(c["分位数3日斜率"]) if pd.notna(c.get("分位数3日斜率")) else None
        item = {
            "数据日期": data_date,
            "可做标记": 0,
            "参与模式": MODE_ROTATION,
            "行业代码": code,
            "行业名称": c.get("行业名称", code),
            "近20日涨幅": None,
            "超额收益率": None,
            "成交额占比_pct": share_pct,
            "占比120日分位数": pct_120,
            "占比3日变化_pct": share_delta,
            "分位数3日斜率": pct_slope,
            "连续3日放量": int(c.get("连续3日放量", 0) or 0),
            "持筹信号": SIGNAL_WATCH,
            "可做得分": 0.0,
            "说明": "资金异动起量，价格趋势未进分析榜，轻仓试探",
        }
        item["可做得分"] = round(_actionable_score(item), 2)
        rows.append(item)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    cand = df[df["可做得分"] > 0].copy()
    if not cand.empty:
        top_n = cand.sort_values("可做得分", ascending=False).head(max(1, max_actionable))
        df.loc[top_n.index, "可做标记"] = 1
    return df.sort_values(["可做标记", "可做得分"], ascending=[False, False]).reset_index(drop=True)


def run_trend_sectors(
    output_dir: Path,
    *,
    min_capacity: float = MIN_CAPACITY_SHARE_PCT,
    max_actionable: int = MAX_ACTIONABLE_SECTORS,
) -> Path:
    analysis_path = output_dir / ANALYSIS_CSV
    volume_path = output_dir / VOLUME_CSV
    crowding_path = output_dir / CROWDING_CSV

    analysis = _load_csv(analysis_path, "量化打标")
    volume = _load_csv(volume_path, "量价信号")
    crowding = _load_csv(crowding_path, "拥挤度")

    dates = {
        str(analysis["数据日期"].iloc[0]),
        str(volume["数据日期"].iloc[0]),
        str(crowding["数据日期"].iloc[0]),
    }
    if len(dates) > 1:
        logging.warning("三表数据日期不一致：%s", ", ".join(sorted(dates)))
    data_date = str(analysis["数据日期"].iloc[0])

    analysis_by_code = analysis.set_index("行业代码").to_dict("index")
    volume_codes = set(volume["行业代码"].astype(str))
    volume_by_code = volume.set_index("行业代码").to_dict("index")
    crowd_by_code = crowding.set_index("行业代码").to_dict("index")

    candidate_codes: set[str] = set(crowding["行业代码"].astype(str))
    candidate_codes |= set(analysis["行业代码"].astype(str))

    # 预筛 B 组复活候选（主线、缺量价、超级容量）
    revival_candidates: list[str] = []
    for code in candidate_codes:
        a = analysis_by_code.get(code)
        c = crowd_by_code.get(code)
        if not a or not c:
            continue
        share_pct = float(c.get("成交额占比_pct", 0) or 0)
        if (
            c.get("拥挤度状态") == STATUS_CROWDED
            and code not in volume_codes
            and share_pct >= SUPER_MAIN_SHARE_PCT
        ):
            revival_candidates.append(code)

    revival_flags: dict[str, bool] = {}
    if revival_candidates:
        sectors = load_sectors_json(output_dir)
        sector_by_code = {_sector_code(s): s for s in sectors}
        code_map = build_sw_index_code_map()
        logging.info("B 组复活检测 %d 个：%s", len(revival_candidates), revival_candidates)
        revival_flags = _fetch_revival_flags(revival_candidates, sector_by_code, code_map)

    capacity_filtered = 0
    rows: list[dict[str, Any]] = []

    for code in candidate_codes:
        a = analysis_by_code.get(code)
        c = crowd_by_code.get(code)
        v = volume_by_code.get(code)
        in_analysis = a is not None
        has_volume = code in volume_codes

        share_pct = float(c["成交额占比_pct"]) if c and pd.notna(c.get("成交额占比_pct")) else None
        passes_cap = _passes_capacity(share_pct, min_capacity)
        if not passes_cap and not (c and c.get("拥挤度状态") == STATUS_EARLY):
            if in_analysis or (c and c.get("拥挤度状态") == STATUS_CROWDED):
                capacity_filtered += 1
            continue

        crowd_status = c.get("拥挤度状态") if c else None
        pct_120 = float(c["占比120日分位数"]) if c and pd.notna(c.get("占比120日分位数")) else None
        share_delta = (
            float(c["占比3日变化_pct"])
            if c and "占比3日变化_pct" in c and pd.notna(c.get("占比3日变化_pct"))
            else None
        )
        pct_slope = (
            float(c["分位数3日斜率"])
            if c and "分位数3日斜率" in c and pd.notna(c.get("分位数3日斜率"))
            else None
        )

        revival = bool(revival_flags.get(code, False))
        tier = _classify_tier(
            in_analysis=in_analysis,
            has_volume=has_volume,
            crowd_status=crowd_status,
            pct_120=pct_120,
            passes_capacity=passes_cap,
            revival=revival,
        )
        if tier is None:
            continue

        if revival:
            hold_signal = SIGNAL_REVIVAL
        elif tier == TIER_EARLY:
            hold_signal = SIGNAL_WATCH
        else:
            hold_signal = _crowding_marginal_signal(pct_120, share_delta, pct_slope)

        name = (a or c or v or {}).get("行业名称", code)
        ret20 = a.get("近20日涨幅") if a else None
        excess = a.get("超额收益率") if a else None
        vol_ratio = v.get("成交额暴增倍数") if v else None
        vol_up = int(c.get("连续3日放量", 0)) if c else 0

        ret20_f = float(ret20) if ret20 is not None and pd.notna(ret20) else None
        participate = _classify_participation(
            in_analysis=in_analysis,
            tier=tier,
            ret20=ret20_f,
            pct_120=pct_120,
            hold_signal=hold_signal,
            crowd_status=crowd_status,
            share_delta=share_delta,
            pct_slope=pct_slope,
            passes_capacity=passes_cap,
        )

        rows.append(
            {
                "数据日期": data_date,
                "参与模式": participate,
                "趋势层级": tier,
                "趋势标记": 0,
                "可做标记": 0,
                "持筹信号": hold_signal,
                "行业代码": code,
                "行业名称": name,
                "近20日涨幅": ret20,
                "超额收益率": excess,
                "成交额占比_pct": share_pct,
                "占比120日分位数": pct_120,
                "占比3日变化_pct": share_delta,
                "分位数3日斜率": pct_slope,
                "拥挤度状态": crowd_status or "—",
                "连续3日放量": vol_up,
                "量价三重": 1 if has_volume else 0,
                "成交额暴增倍数": vol_ratio,
                "说明": _tier_note(tier, bool(vol_up), hold_signal, revival),
                "出局阈值": _exit_threshold_text(share_pct, pct_120),
            }
        )

    if capacity_filtered:
        logging.info("容量门槛 <%.1f%% 排除 %d 个（含非金属材料/玻璃玻纤等小板块）", min_capacity, capacity_filtered)

    if not rows:
        raise RuntimeError("合并后无趋势行业")

    df = pd.DataFrame(rows)
    df["趋势标记"] = _pick_trend_mark(df)

    actionable = _build_actionable_universe(
        analysis, crowding, data_date, min_capacity=min_capacity, max_actionable=max_actionable
    )
    act_path = output_dir / ACTIONABLE_CSV
    actionable.to_csv(act_path, index=False, encoding="utf-8-sig")

    act_mark = actionable[actionable["可做标记"] == 1]
    for _, act_row in act_mark.iterrows():
        act_code = str(act_row["行业代码"])
        df.loc[df["行业代码"] == act_code, "可做标记"] = 1
        if act_code not in set(df["行业代码"].astype(str)):
            logging.info(
                "可做行业 %s 不在 trend 分层表，详见 %s",
                act_row["行业名称"],
                act_path.name,
            )

    df["_participate_rank"] = df["参与模式"].map(
        {MODE_ACTIONABLE: 0, MODE_ROTATION: 1, MODE_EXTENDED: 2}
    ).fillna(3)
    df["_tier_rank"] = df["趋势层级"].map(TIER_ORDER)
    df["_ret"] = pd.to_numeric(df["近20日涨幅"], errors="coerce").fillna(-1)
    df = df.sort_values(
        ["可做标记", "趋势标记", "_participate_rank", "_tier_rank", "_ret"],
        ascending=[False, False, True, True, False],
    ).drop(columns=["_participate_rank", "_tier_rank", "_ret"])
    df = df[list(RESULT_COLUMNS)].reset_index(drop=True)

    out_path = output_dir / RESULT_CSV
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    trend = df[df["趋势标记"] == 1]
    if not trend.empty:
        t = trend.iloc[0]
        logging.info("趋势方向: %s (%s) [%s]", t["行业名称"], t["趋势层级"], t["参与模式"])

    if not act_mark.empty:
        logging.info("今日可做行业 %d 个（最多 %d）：", len(act_mark), max_actionable)
        for _, a in act_mark.iterrows():
            logging.info(
                "  ★ %s 得分=%.1f 20d=%.1f%% 分位=%.0f%% [%s]",
                a["行业名称"],
                float(a["可做得分"]),
                float(a["近20日涨幅"] or 0),
                float(a["占比120日分位数"] or 0),
                a["参与模式"],
            )
    else:
        logging.warning("今日无可做行业（全市场延伸或容量不足），详见 %s", act_path.name)

    act_show = actionable[actionable["可做得分"] > 0].head(5)
    for _, r in act_show.iterrows():
        flag = "★" if r["可做标记"] == 1 else " "
        logging.info(
            "  %s %s %s 得分=%.1f 20d=%s 分位=%s 占比Δ3d=%s",
            flag,
            r["行业代码"],
            r["行业名称"],
            float(r["可做得分"]),
            r["近20日涨幅"],
            r["占比120日分位数"],
            r["占比3日变化_pct"],
        )

    for tier in (TIER_CORE, TIER_REVIVAL, TIER_MAIN, TIER_VOLUME, TIER_EARLY, TIER_WATCH):
        n = int((df["趋势层级"] == tier).sum())
        if n:
            logging.info("  %s: %d 个", tier, n)
    logging.info("完成 → %s + %s", out_path, act_path)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="申万二级趋势行业综合（三表合并 + 四维优化）")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--min-capacity",
        type=float,
        default=MIN_CAPACITY_SHARE_PCT,
        help=f"成交额占比硬门槛（%%），默认 {MIN_CAPACITY_SHARE_PCT}",
    )
    parser.add_argument(
        "--max-actionable",
        type=int,
        default=MAX_ACTIONABLE_SECTORS,
        help=f"今日「可做标记=1」行业个数上限（按可做得分 Top N），默认 {MAX_ACTIONABLE_SECTORS}",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        path = run_trend_sectors(
            args.output_dir.resolve(),
            min_capacity=args.min_capacity,
            max_actionable=max(1, args.max_actionable),
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("执行失败：%s", exc)
        sys.exit(1)

    print(f"结果文件：{path}")


if __name__ == "__main__":
    main()
