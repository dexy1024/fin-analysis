#!/usr/bin/env python3
"""
申万二级 + observation/watchlist + 快照 · 端到端可买清单。

标的池（并集）：
  1) backend/data/watchlist.json 持仓
  2) backend/data/observation.json 观察池（不含港股，与快照一致）
  3) 申万流水线：可做行业下 sector_leaders 扫描成分 + 入选龙头

快照：logs/snapshots_*_new.csv 最新一批（四条件 + 日线风控）

输出：
  - 09_shenwan_v2_buyable_e2e.csv
  - logs/shenwan_v2_buyable_e2e.json

用法（仓库根目录）：
  python3 backend/scripts/pick_buyable_e2e.py -o .
  ./pick_buyable_e2e.sh
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from services.observation_data import (  # noqa: E402
    load_observation_items,
    WATCHLIST_FILE,
)

ACTIONABLE_CSV = "07_shenwan_v2_actionable_sectors.csv"
LEADERS_CSV = "08_shenwan_v2_sector_leaders.csv"
TREND_CSV = "06_shenwan_v2_trend_sectors.csv"
OUT_CSV = "09_shenwan_v2_buyable_e2e.csv"
OUT_JSON = "logs/shenwan_v2_buyable_e2e.json"

TIER_BUY = "A_可买入"
TIER_WATCH = "B_盯盘"
TIER_SKIP = "X_排除"

RESULT_COLUMNS = (
    "数据日期",
    "层级",
    "来源",
    "是否持仓",
    "代码",
    "名称",
    "行业代码",
    "行业名称",
    "行业可做",
    "申万龙头",
    "距MA20_pct",
    "现价",
    "日线风控",
    "实际交易动作",
    "60m交易",
    "客观缠论信号",
    "60m笔方向",
    "15分信号",
    "区间价格对齐",
    "缺少条件",
    "说明",
)

SNAPSHOT_GLOB = "snapshots_*_new.csv"

SOURCE_WATCHLIST = "持仓"
SOURCE_OBSERVATION = "观察池"
SOURCE_SHENWAN_LEADER = "申万龙头"
SOURCE_SHENWAN_SCAN = "申万扫描"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _latest_data_date(rows: list[dict[str, str]], key: str = "数据日期") -> str | None:
    dates = [r.get(key, "").strip() for r in rows if r.get(key, "").strip()]
    return max(dates) if dates else None


def _filter_date(rows: list[dict[str, str]], data_date: str | None) -> list[dict[str, str]]:
    if not data_date:
        return rows
    return [r for r in rows if r.get("数据日期", "").strip() == data_date]


def _find_snapshot_csv(root: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_file() else None
    logs = root / "logs"
    candidates = sorted(logs.glob(SNAPSHOT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_latest_snapshots(path: Path) -> tuple[str | None, dict[str, dict[str, str]]]:
    rows = _read_csv(path)
    if not rows:
        return None, {}
    latest_ts = max(r.get("时间", "") for r in rows)
    batch = [r for r in rows if r.get("时间", "") == latest_ts]
    by_code = {r["代码"].strip(): r for r in batch if r.get("代码")}
    return latest_ts, by_code


def _load_watchlist_codes() -> dict[str, str]:
    """code -> name（仅 watchlist.json holdings）。"""
    out: dict[str, str] = {}
    if not WATCHLIST_FILE.is_file():
        return out
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        for item in data.get("holdings", []):
            if isinstance(item, dict) and item.get("code"):
                code = str(item["code"]).strip()
                out[code] = str(item.get("name", "")).strip()
    except (OSError, json.JSONDecodeError, TypeError):
        logging.warning("读取 watchlist.json 失败")
    return out


def _load_pool_symbols() -> dict[str, dict[str, Any]]:
    """
    合并标的池：code -> {name, sources: set[str]}。
    观察池仅 A 股/ETF（observation.json），与 generate_snapshots 一致。
    """
    pool: dict[str, dict[str, Any]] = {}

    def _add(code: str, name: str, source: str) -> None:
        if not code:
            return
        if code not in pool:
            pool[code] = {"name": name, "sources": set()}
        if name and not pool[code]["name"]:
            pool[code]["name"] = name
        pool[code]["sources"].add(source)

    for code, name in _load_watchlist_codes().items():
        _add(code, name, SOURCE_WATCHLIST)

    for item in load_observation_items(include_hk=False):
        _add(item["code"], item.get("name", ""), SOURCE_OBSERVATION)

    return pool


def _format_sources(sources: set[str], *, is_leader: bool) -> str:
    order = (
        SOURCE_WATCHLIST,
        SOURCE_OBSERVATION,
        SOURCE_SHENWAN_LEADER,
        SOURCE_SHENWAN_SCAN,
    )
    parts = [s for s in order if s in sources]
    if is_leader and SOURCE_SHENWAN_LEADER not in parts:
        parts.append(SOURCE_SHENWAN_LEADER)
    return "+".join(parts)


def _missing_buy_conditions(row: dict[str, str]) -> list[str]:
    miss: list[str] = []
    chan = row.get("客观缠论信号", "")
    if "卖" in chan:
        miss.append("客观信号含卖")
    if row.get("60m笔方向") != "向下":
        miss.append("60m笔须向下")
    if row.get("15分信号") != "底背驰":
        miss.append("15m底背驰")
    if row.get("区间价格对齐") != "是":
        miss.append("区间对齐")
    if row.get("日线风控") != "安全":
        miss.append(row.get("日线风控") or "日线风控")
    if row.get("大盘状态") == "警戒":
        miss.append("大盘警戒")
    return miss


def _tier_from_snapshot(
    snap: dict[str, str] | None,
    *,
    in_pool: bool,
    is_leader: bool,
    sector_ok: bool,
) -> tuple[str, list[str], str]:
    if snap is None:
        if in_pool:
            hint = "申万回踩龙头" if is_leader else "观察/持仓池标的"
            return TIER_WATCH, ["无快照数据"], f"{hint}，需先 ./generate_snapshots.sh --write"
        return TIER_SKIP, ["无快照"], "不在标的池"

    miss = _missing_buy_conditions(snap)
    act = snap.get("实际交易动作", "")
    risk = snap.get("日线风控", "")

    if act == "买入" and risk == "安全":
        return TIER_BUY, [], "四条件齐全，日线未破 min(A-ZD,C-ZD)"

    if risk == "日线破位":
        return TIER_SKIP, miss, "跌破日线战略底线 min(A-ZD,C-ZD)，不宜新开仓"

    if "客观信号含卖" in miss:
        return TIER_SKIP, miss, "60m 结构含卖，与开仓方向冲突"

    pool_note = "观察/持仓池" if in_pool and not is_leader else ""
    if is_leader and sector_ok:
        if snap.get("60m交易") == "买入":
            return TIER_WATCH, miss, "申万回踩龙头 + 60m 就绪，等 15m 底背驰与区间对齐"
        return TIER_WATCH, miss, "申万回踩龙头，缠论买点未齐"

    if in_pool:
        if snap.get("60m交易") == "买入" and risk == "安全":
            prefix = f"{pool_note} " if pool_note else ""
            return TIER_WATCH, miss, f"{prefix}60m 就绪，等 15m 底背驰与区间对齐".strip()
        if snap.get("15分信号") == "底背驰" and risk == "安全" and "客观信号含卖" not in miss:
            return TIER_WATCH, miss, f"{pool_note} 15m 底背驰已有，等其余条件".strip()
        return TIER_WATCH, miss, f"{pool_note} 缠论买点未齐，继续观望".strip()

    return TIER_SKIP, miss, "未满足端到端筛选"


def _load_shenwan(root: Path) -> dict[str, Any]:
    actionable_rows = _read_csv(root / ACTIONABLE_CSV)
    leaders_rows = _read_csv(root / LEADERS_CSV)
    trend_rows = _read_csv(root / TREND_CSV)

    data_date = _latest_data_date(actionable_rows) or _latest_data_date(leaders_rows)
    actionable_rows = _filter_date(actionable_rows, data_date)
    leaders_rows = _filter_date(leaders_rows, data_date)
    trend_rows = _filter_date(trend_rows, data_date)

    actionable_codes = {
        r["行业代码"].strip()
        for r in actionable_rows
        if r.get("可做标记", "").strip() == "1"
    }
    actionable_names = {
        r["行业代码"].strip(): r.get("行业名称", "")
        for r in actionable_rows
        if r.get("行业代码")
    }
    trend_mark = next(
        (r for r in trend_rows if r.get("趋势标记", "").strip() == "1"),
        None,
    )

    leaders_by_code: dict[str, dict[str, str]] = {}
    pipeline_codes: set[str] = set()
    for r in leaders_rows:
        code = r.get("股票代码", "").strip()
        if not code:
            continue
        leaders_by_code[code] = r
        sector = r.get("行业代码", "").strip()
        if sector in actionable_codes or r.get("入选龙头", "").strip() == "1":
            pipeline_codes.add(code)

    return {
        "data_date": data_date,
        "actionable_codes": actionable_codes,
        "actionable_names": actionable_names,
        "trend_mark": trend_mark,
        "leaders_by_code": leaders_by_code,
        "pipeline_codes": pipeline_codes,
    }


def _merge_pool_with_shenwan(
    pool: dict[str, dict[str, Any]],
    sw: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    leaders_by_code: dict[str, dict[str, str]] = sw["leaders_by_code"]
    pipeline_codes: set[str] = sw["pipeline_codes"]

    for code in pipeline_codes:
        leader = leaders_by_code.get(code, {})
        name = leader.get("股票名称", "")
        if code not in pool:
            pool[code] = {"name": name, "sources": set()}
        elif name and not pool[code]["name"]:
            pool[code]["name"] = name
        if leader.get("入选龙头", "").strip() == "1":
            pool[code]["sources"].add(SOURCE_SHENWAN_LEADER)
        else:
            pool[code]["sources"].add(SOURCE_SHENWAN_SCAN)

    return pool


def build_e2e_rows(root: Path, snapshot_path: Path | None) -> dict[str, Any]:
    pool = _load_pool_symbols()
    sw = _load_shenwan(root)
    pool = _merge_pool_with_shenwan(pool, sw)

    snap_file = _find_snapshot_csv(root, snapshot_path)
    snap_ts: str | None = None
    snap_by_code: dict[str, dict[str, str]] = {}
    if snap_file:
        snap_ts, snap_by_code = _load_latest_snapshots(snap_file)

    data_date = sw["data_date"]
    actionable_codes: set[str] = sw["actionable_codes"]
    actionable_names: dict[str, str] = sw["actionable_names"]
    leaders_by_code: dict[str, dict[str, str]] = sw["leaders_by_code"]
    watchlist_codes = set(_load_watchlist_codes())

    out_rows: list[dict[str, str]] = []
    for code in sorted(pool.keys()):
        meta = pool[code]
        leader = leaders_by_code.get(code)
        snap = snap_by_code.get(code)
        is_leader = leader is not None and leader.get("入选龙头", "").strip() == "1"
        sector_code = (leader or {}).get("行业代码", "").strip()
        sector_name = (leader or {}).get("行业名称", "")
        sector_ok = bool(sector_code and sector_code in actionable_codes)
        sources: set[str] = set(meta["sources"])
        if is_leader:
            sources.add(SOURCE_SHENWAN_LEADER)

        tier, miss, note = _tier_from_snapshot(
            snap,
            in_pool=True,
            is_leader=is_leader,
            sector_ok=sector_ok or is_leader,
        )

        name = meta.get("name") or (leader or {}).get("股票名称") or (snap or {}).get("名称", "")
        is_holding = "是" if code in watchlist_codes else "否"
        if snap and snap.get("是否持仓") == "是":
            is_holding = "是"

        out_rows.append(
            {
                "数据日期": data_date or "",
                "层级": tier,
                "来源": _format_sources(sources, is_leader=is_leader),
                "是否持仓": is_holding,
                "代码": code,
                "名称": name,
                "行业代码": sector_code,
                "行业名称": sector_name,
                "行业可做": "1" if sector_ok else "0",
                "申万龙头": "1" if is_leader else "0",
                "距MA20_pct": (leader or {}).get("距MA20_pct", ""),
                "现价": (snap or {}).get("现价", ""),
                "日线风控": (snap or {}).get("日线风控", ""),
                "实际交易动作": (snap or {}).get("实际交易动作", ""),
                "60m交易": (snap or {}).get("60m交易", ""),
                "客观缠论信号": (snap or {}).get("客观缠论信号", ""),
                "60m笔方向": (snap or {}).get("60m笔方向", ""),
                "15分信号": (snap or {}).get("15分信号", ""),
                "区间价格对齐": (snap or {}).get("区间价格对齐", ""),
                "缺少条件": "；".join(miss) if miss else "",
                "说明": note,
            }
        )

    order = {TIER_BUY: 0, TIER_WATCH: 1, TIER_SKIP: 2}
    out_rows.sort(
        key=lambda r: (
            order.get(r["层级"], 9),
            r["申万龙头"] != "1",
            r["是否持仓"] != "是",
            r["代码"],
        )
    )

    pool_stats = {
        "持仓": sum(1 for c in pool if SOURCE_WATCHLIST in pool[c]["sources"]),
        "观察池": sum(1 for c in pool if SOURCE_OBSERVATION in pool[c]["sources"]),
        "申万龙头": sum(
            1 for c in pool if SOURCE_SHENWAN_LEADER in pool[c]["sources"]
        ),
        "申万扫描": sum(1 for c in pool if SOURCE_SHENWAN_SCAN in pool[c]["sources"]),
        "合计去重": len(pool),
    }

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "shenwan_data_date": data_date,
        "snapshot_file": str(snap_file) if snap_file else None,
        "snapshot_time": snap_ts,
        "标的池统计": pool_stats,
        "可做行业": [
            {"code": c, "name": actionable_names.get(c, "")} for c in sorted(actionable_codes)
        ],
        "趋势方向": (
            {
                "行业": sw["trend_mark"].get("行业名称"),
                "代码": sw["trend_mark"].get("行业代码"),
                "参与模式": sw["trend_mark"].get("参与模式"),
            }
            if sw.get("trend_mark")
            else None
        ),
        "counts": {
            TIER_BUY: sum(1 for r in out_rows if r["层级"] == TIER_BUY),
            TIER_WATCH: sum(1 for r in out_rows if r["层级"] == TIER_WATCH),
            TIER_SKIP: sum(1 for r in out_rows if r["层级"] == TIER_SKIP),
        },
        "可买入": [r for r in out_rows if r["层级"] == TIER_BUY],
        "盯盘": [r for r in out_rows if r["层级"] == TIER_WATCH],
        "排除": [r for r in out_rows if r["层级"] == TIER_SKIP],
    }
    return {"rows": out_rows, "summary": summary}


def write_outputs(root: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    csv_path = root / OUT_CSV
    json_path = root / OUT_JSON
    json_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = payload["rows"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload["summary"], f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def _print_summary(payload: dict[str, Any]) -> None:
    s = payload["summary"]
    print("")
    print("===== 端到端可买清单 =====")
    print(f"申万数据日期: {s.get('shenwan_data_date')}")
    print(f"快照文件: {s.get('snapshot_file')}")
    print(f"快照时间: {s.get('snapshot_time')}")
    stats = s.get("标的池统计") or {}
    print(
        f"标的池: 合计 {stats.get('合计去重', 0)} "
        f"(持仓 {stats.get('持仓', 0)} + 观察 {stats.get('观察池', 0)} "
        f"+ 申万龙头/扫描)"
    )
    trend = s.get("趋势方向")
    if trend:
        print(f"趋势风向标: {trend.get('行业')} ({trend.get('参与模式')}) — 不追")
    sectors = s.get("可做行业") or []
    if sectors:
        names = "、".join(x["name"] for x in sectors)
        print(f"今日可做行业: {names}")
    counts = s.get("counts") or {}
    print(
        f"A_可买入: {counts.get(TIER_BUY, 0)}  "
        f"B_盯盘: {counts.get(TIER_WATCH, 0)}  "
        f"X_排除: {counts.get(TIER_SKIP, 0)}"
    )

    for label, key in (
        ("【可买入】", "可买入"),
        ("【盯盘】（节选 Top15）", "盯盘"),
        ("【排除】（节选 Top10）", "排除"),
    ):
        items = s.get(key) or []
        if not items:
            print(f"\n{label} （无）")
            continue
        print(f"\n{label}")
        limit = 15 if key == "盯盘" else 10
        for r in items[:limit]:
            print(
                f"  [{r.get('来源','')}] {r['代码']} {r['名称']} "
                f"持仓={r.get('是否持仓')} "
                f"| 实际={r.get('实际交易动作','-')} 60m={r.get('60m交易','-')} "
                f"风控={r.get('日线风控','-')} "
                f"| 缺: {r.get('缺少条件') or '无'}"
            )
        if len(items) > limit:
            print(f"  … 另有 {len(items) - limit} 只，见 {OUT_CSV}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="观察池+持仓+申万 · 端到端可买清单")
    p.add_argument("-o", "--output-dir", default=".", help="仓库根目录")
    p.add_argument(
        "--snapshots",
        type=Path,
        default=None,
        help="快照 CSV（默认 logs/snapshots_*_new.csv 最新）",
    )
    args = p.parse_args()
    root = Path(args.output_dir).resolve()

    payload = build_e2e_rows(root, args.snapshots)
    csv_path, json_path = write_outputs(root, payload)
    logging.info("已写入 %s (%d 行)", csv_path, len(payload["rows"]))
    logging.info("已写入 %s", json_path)
    _print_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
