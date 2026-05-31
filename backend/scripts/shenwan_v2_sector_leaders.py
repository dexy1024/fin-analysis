#!/usr/bin/env python3
"""
申万二级 · 第二层战术个股：回踩可接版（不追暴涨、行业过热则不开新仓）。

哲学：
  - 第一层「趋势方向」≠ 今日可做；第二层处理全部「可做标记=1」的行业（默认可多个）。
  - 行业 gate 仍作安全阀（持筹恶化 / 极端延伸）。

个股筛选（gate 通过时）：
  1. 成交额 Top TOP_LIQUID
  2. 距 MA20 在 [MA20_PULLBACK_MIN, MA20_PULLBACK_MAX]（默认 -3% ~ +8%，回踩区）
  3. 20 日涨幅 < 行业 + STOCK_EXCESS_MAX，且 > 行业 + STOCK_LAG_MIN（避免死票）
  4. 按「越靠近 MA20 + 流动性越好」排序，取 2~3 只

用法：
    python3 backend/scripts/shenwan_v2_sector_leaders.py -o .
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from services.index_cache import load_a_share_daily_dataframe  # noqa: E402
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS  # noqa: E402
from shenwan_v2_trend_sectors import ACTIONABLE_CSV, RESULT_CSV as TREND_CSV  # noqa: E402

SECTORS_JSON = "shenwan_v2_sectors.json"
RESULT_CSV = "08_shenwan_v2_sector_leaders.csv"
OBSERVATION_FILE = _BACKEND_DIR / "data" / "observation.json"
LEADERS_META_FILE = "logs/shenwan_v2_sector_leaders_meta.json"

TOP_LIQUID = 10
MAX_LEADERS = 3
MIN_LEADERS = 1
RETURN_WINDOW = 20
MA20_WINDOW = 20

# 行业 gate：过热则今日不新开
SECTOR_RET20_MAX = 28.0
SECTOR_PCT120_MAX = 98.0
HOLD_SIGNAL_BLOCK = frozenset({"警惕撤离", "建议出局"})

# 个股：回踩 MA20 接人区
MA20_PULLBACK_MIN = -3.0
MA20_PULLBACK_MAX = 8.0
STOCK_EXCESS_MAX = 8.0
STOCK_LAG_MIN = -15.0

FETCH_SLEEP_SEC = 0.08
MAX_WORKERS = 4

MODE_TRADE = "可接"
MODE_WATCH = "观望"

RESULT_COLUMNS = (
    "数据日期",
    "战术模式",
    "参与模式",
    "持筹信号",
    "行业代码",
    "行业名称",
    "行业20日涨幅",
    "占比120日分位数",
    "股票代码",
    "股票名称",
    "当日成交额",
    "近20日涨幅",
    "超额动量",
    "距MA20_pct",
    "流动性排名",
    "入选龙头",
    "筛选说明",
)


def _to_symbol(stock_code: str) -> str | None:
    s = stock_code.strip().lower()
    m = re.fullmatch(r"(?:sh|sz|bj)?(\d{6})", s)
    if not m:
        return None
    sym = m.group(1)
    if sym.startswith(("0", "3", "6")):
        return sym
    return None


def _load_sectors_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    sectors = data.get("sectors", [])
    if not isinstance(sectors, list):
        raise ValueError(f"{path} 格式无效")
    return sectors


def _find_sector(sectors: list[dict[str, Any]], sector_code: str) -> dict[str, Any] | None:
    for sec in sectors:
        if str(sec.get("sector_code", "")).strip() == sector_code:
            return sec
    return None


def _row_to_actionable_primary(row: pd.Series) -> dict[str, Any]:
    ret = row.get("近20日涨幅")
    if pd.isna(ret):
        raise RuntimeError(f"可做行业 {row['行业名称']} 缺少近20日涨幅")
    pct = row.get("占比120日分位数")
    return {
        "sector_code": str(row["行业代码"]),
        "sector_name": str(row["行业名称"]),
        "sector_ret20": float(ret),
        "pct_120": float(pct) if pd.notna(pct) else None,
        "hold_signal": str(row.get("持筹信号", "")),
        "participate_mode": str(row.get("参与模式", "")),
        "actionable_score": float(row.get("可做得分", 0) or 0),
        "data_date": str(row["数据日期"]),
    }


def _load_actionable_sectors(output_dir: Path) -> list[dict[str, Any]]:
    """从「可做」清单取今日参与行业（可多行业），而非延伸段趋势方向。"""
    act_path = output_dir / ACTIONABLE_CSV
    if act_path.is_file():
        df = pd.read_csv(act_path)
        marked = df[df["可做标记"] == 1].sort_values("可做得分", ascending=False)
        if not marked.empty:
            return [_row_to_actionable_primary(row) for _, row in marked.iterrows()]

    trend_path = output_dir / TREND_CSV
    df = pd.read_csv(trend_path)
    if df.empty:
        raise RuntimeError(f"{trend_path} 为空")
    marked = (
        df[df["可做标记"] == 1].sort_values("近20日涨幅", ascending=False)
        if "可做标记" in df.columns
        else pd.DataFrame()
    )
    if marked.empty:
        raise RuntimeError("今日无可做行业（延伸段或容量不足），第二层不新开")
    return [_row_to_actionable_primary(row) for _, row in marked.iterrows()]


def _load_actionable_sector(output_dir: Path) -> dict[str, Any]:
    """兼容：返回第一个可做行业。"""
    sectors = _load_actionable_sectors(output_dir)
    return sectors[0]


def _sector_entry_gate(primary: dict[str, Any]) -> tuple[bool, str]:
    """返回 (允许接人, 原因)。"""
    ret = primary["sector_ret20"]
    pct = primary.get("pct_120")
    sig = primary.get("hold_signal", "")

    if sig in HOLD_SIGNAL_BLOCK:
        return False, f"行业持筹信号={sig}，资金边际转弱"
    if ret > SECTOR_RET20_MAX:
        return False, f"行业20日已+{ret:.1f}%（>{SECTOR_RET20_MAX:.0f}%），延伸段不宜日线新开"
    if pct is not None and pct >= SECTOR_PCT120_MAX:
        return False, f"拥挤分位{pct:.0f}%（≥{SECTOR_PCT120_MAX:.0f}%），等回踩或换层"
    return True, "行业仍在可参与区间"


def _pct_return(close: pd.Series, window: int = RETURN_WINDOW) -> float | None:
    if len(close) < window + 1:
        return None
    start = float(close.iloc[-(window + 1)])
    end = float(close.iloc[-1])
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _dist_from_ma20_pct(close: pd.Series) -> float | None:
    if len(close) < MA20_WINDOW:
        return None
    ma20 = float(close.rolling(MA20_WINDOW).mean().iloc[-1])
    last = float(close.iloc[-1])
    if ma20 <= 0:
        return None
    return (last / ma20 - 1.0) * 100.0


def _fetch_stock_metrics(stock_code: str, stock_name: str) -> dict[str, Any] | None:
    sym = _to_symbol(stock_code)
    if not sym:
        return None
    try:
        df = load_a_share_daily_dataframe(sym, force_refresh=False)
    except EXPECTED_BUSINESS_EXCEPTIONS:
        try:
            df = load_a_share_daily_dataframe(sym, force_refresh=True)
        except EXPECTED_BUSINESS_EXCEPTIONS:
            return None
        except Exception:
            logging.exception("sector_leaders: %s 强制刷新未预期异常", sym)
            raise
    except Exception:
        logging.exception("sector_leaders: %s 读取日线未预期异常", sym)
        raise
    if df is None or df.empty or len(df) < RETURN_WINDOW + 1:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    amount_today = float(close.iloc[-1] * volume.iloc[-1])
    if amount_today <= 0 or pd.isna(amount_today):
        return None
    ret20 = _pct_return(close)
    ma20_dist = _dist_from_ma20_pct(close)
    if ret20 is None:
        return None

    return {
        "股票代码": sym,
        "股票名称": stock_name,
        "当日成交额": amount_today,
        "近20日涨幅": round(ret20, 4),
        "距MA20_pct": round(ma20_dist, 4) if ma20_dist is not None else None,
    }


def _pick_pullback_leaders(
    stocks: list[dict[str, str]],
    sector_ret20: float,
    *,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics: list[dict[str, Any]] = []
    workers = max(1, min(workers, MAX_WORKERS))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_stock_metrics, s["stock_code"], s["stock_name"]): s for s in stocks
        }
        for fut in as_completed(futures):
            row = fut.result()
            if row:
                metrics.append(row)
            time.sleep(FETCH_SLEEP_SEC / max(workers, 1))

    if not metrics:
        raise RuntimeError("成分股均无有效行情")

    pool = sorted(metrics, key=lambda x: x["当日成交额"], reverse=True)[:TOP_LIQUID]
    for i, row in enumerate(pool, start=1):
        row["流动性排名"] = i
        row["超额动量"] = round(float(row["近20日涨幅"]) - sector_ret20, 4)
        ma = row.get("距MA20_pct")
        excess = float(row["超额动量"])
        ret = float(row["近20日涨幅"])

        if ma is None:
            row["筛选说明"] = "排除：无法计算MA20"
            row["_ok"] = False
            continue
        if not (MA20_PULLBACK_MIN <= ma <= MA20_PULLBACK_MAX):
            row["筛选说明"] = f"排除：距MA20 {ma:+.1f}% 不在回踩区[{MA20_PULLBACK_MIN},{MA20_PULLBACK_MAX}]"
            row["_ok"] = False
            continue
        if excess > STOCK_EXCESS_MAX:
            row["筛选说明"] = f"排除：超额+{excess:.1f}pct>{STOCK_EXCESS_MAX}（仍偏热）"
            row["_ok"] = False
            continue
        if excess < STOCK_LAG_MIN:
            row["筛选说明"] = f"排除：跑输行业过多({excess:.1f}pct)"
            row["_ok"] = False
            continue
        row["_ok"] = True
        row["筛选说明"] = f"回踩可接：MA20 {ma:+.1f}%，超额{excess:+.1f}pct"

    ok = [r for r in pool if r.get("_ok")]
    ok.sort(key=lambda x: (abs(float(x["距MA20_pct"] or 0)), x["流动性排名"]))

    leaders: list[dict[str, Any]] = []
    for r in ok[:MAX_LEADERS]:
        leaders.append({k: v for k, v in r.items() if not str(k).startswith("_")})

    if len(leaders) < MIN_LEADERS:
        logging.warning("回踩区内仅 %d 只达标（<%d）", len(leaders), MIN_LEADERS)

    leader_codes = {r["股票代码"] for r in leaders}
    for r in pool:
        if r["股票代码"] not in leader_codes and r.get("_ok"):
            r["筛选说明"] = r.get("筛选说明", "") + "（未入选名额）"

    clean_pool = [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in pool]
    return clean_pool, leaders


def _load_managed_codes(output_dir: Path) -> list[str]:
    path = output_dir / LEADERS_META_FILE
    if not path.is_file():
        return []
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        codes: list[str] = []
        for key in ("observation_managed", "observation_added"):
            codes.extend(str(c) for c in meta.get(key, []))
        for item in meta.get("龙头", []):
            if isinstance(item, dict) and item.get("code"):
                codes.append(str(item["code"]))
        for sec in meta.get("行业列表", []):
            if isinstance(sec, dict):
                for item in sec.get("龙头", []):
                    if isinstance(item, dict) and item.get("code"):
                        codes.append(str(item["code"]))
        return list(dict.fromkeys(c for c in codes if c))
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _sync_observation(
    leaders: list[dict[str, Any]],
    managed_prev: list[str],
    *,
    dry_run: bool,
) -> tuple[list[str], list[str], list[str]]:
    """移除脚本托管的上轮标的；本轮若有龙头再写入。"""
    if not OBSERVATION_FILE.is_file():
        raise FileNotFoundError(f"未找到 {OBSERVATION_FILE}")

    new_codes = {str(r["股票代码"]).strip() for r in leaders}
    prev = set(managed_prev)

    data = json.loads(OBSERVATION_FILE.read_text(encoding="utf-8"))
    observations: list[dict[str, str]] = list(data.get("observations", []))

    removed: list[str] = []
    kept: list[dict[str, str]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if code in prev and code not in new_codes:
            removed.append(code)
            logging.info("observation 移除战术托管 %s", code)
            continue
        kept.append({"code": code, "name": str(item.get("name", "")).strip()})

    added: list[str] = []
    existing = {o["code"] for o in kept}
    for row in leaders:
        code = str(row["股票代码"]).strip()
        name = str(row["股票名称"]).strip()
        if code in existing:
            continue
        kept.append({"code": code, "name": name})
        existing.add(code)
        added.append(code)
        logging.info("observation 新增 %s %s", code, name)

    if not dry_run:
        data["observations"] = kept
        OBSERVATION_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    managed_now = list(new_codes)
    return removed, added, managed_now


def _save_meta(output_dir: Path, meta: dict[str, Any], *, dry_run: bool) -> None:
    path = output_dir / LEADERS_META_FILE
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _process_one_sector(
    output_dir: Path,
    primary: dict[str, Any],
    sectors: list[dict[str, Any]],
    *,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """单行业扫股，返回 (csv行, 入选龙头, 行业摘要)。"""
    code = primary["sector_code"]
    can_enter, gate_reason = _sector_entry_gate(primary)
    sector = _find_sector(sectors, code)
    if not sector:
        raise RuntimeError(f"未在 sectors.json 找到 {code}")

    sector_ret20 = primary["sector_ret20"]
    mode = MODE_TRADE if can_enter else MODE_WATCH
    data_date = primary["data_date"]

    logging.info(
        "战术层 %s (%s) [%s] 得分=%.1f | 模式=%s | %s",
        primary["sector_name"],
        code,
        primary.get("participate_mode", ""),
        primary.get("actionable_score", 0),
        mode,
        gate_reason,
    )

    rows: list[dict[str, Any]] = []
    leaders: list[dict[str, Any]] = []

    if can_enter:
        stocks = sector.get("stocks") or []
        liquid_pool, leaders = _pick_pullback_leaders(stocks, sector_ret20, workers=workers)
        leader_codes = {r["股票代码"] for r in leaders}
        for r in liquid_pool:
            rows.append(
                {
                    "数据日期": data_date,
                    "战术模式": mode,
                    "参与模式": primary.get("participate_mode", ""),
                    "持筹信号": primary.get("hold_signal", ""),
                    "行业代码": code,
                    "行业名称": sector.get("sector_name", ""),
                    "行业20日涨幅": round(sector_ret20, 4),
                    "占比120日分位数": primary.get("pct_120"),
                    **{k: r[k] for k in r if k not in ("_ok",)},
                    "入选龙头": 1 if r["股票代码"] in leader_codes else 0,
                }
            )
        logging.info("  %s 流动性 Top%d（★=回踩可接）：", primary["sector_name"], TOP_LIQUID)
        for r in liquid_pool:
            flag = "★" if r["股票代码"] in leader_codes else " "
            logging.info(
                "    %s #%d %s %s MA20=%+.1f%% | %s",
                flag,
                r["流动性排名"],
                r["股票代码"],
                r["股票名称"],
                r.get("距MA20_pct") or 0,
                r.get("筛选说明", ""),
            )
    else:
        rows.append(
            {
                "数据日期": data_date,
                "战术模式": mode,
                "参与模式": primary.get("participate_mode", ""),
                "持筹信号": primary.get("hold_signal", ""),
                "行业代码": code,
                "行业名称": primary.get("sector_name", sector.get("sector_name", "")),
                "行业20日涨幅": round(sector_ret20, 4),
                "占比120日分位数": primary.get("pct_120"),
                "股票代码": "",
                "股票名称": "",
                "当日成交额": None,
                "近20日涨幅": None,
                "超额动量": None,
                "距MA20_pct": None,
                "流动性排名": None,
                "入选龙头": 0,
                "筛选说明": gate_reason + "；今日不写入 observation，等 60m 缠论或行业回调",
            }
        )
        logging.info("  %s 观望：%s", primary["sector_name"], gate_reason)

    summary = {
        "行业代码": code,
        "行业名称": primary["sector_name"],
        "战术模式": mode,
        "gate_reason": gate_reason,
        "可做得分": primary.get("actionable_score", 0),
        "龙头": [{"code": r["股票代码"], "name": r["股票名称"]} for r in leaders],
    }
    return rows, leaders, summary


def run_sector_leaders(
    output_dir: Path,
    *,
    sector_code: str | None = None,
    dry_run: bool = False,
    workers: int = MAX_WORKERS,
    apply_observation: bool = True,
) -> Path:
    trend_path = output_dir / TREND_CSV
    sectors_path = output_dir / SECTORS_JSON
    if not trend_path.is_file():
        raise FileNotFoundError(f"缺少 {trend_path}")
    if not sectors_path.is_file():
        raise FileNotFoundError(f"缺少 {sectors_path}")

    primaries = _load_actionable_sectors(output_dir)
    if sector_code:
        primaries = [p for p in primaries if p["sector_code"] == sector_code]
        if not primaries:
            raise RuntimeError(f"行业 {sector_code} 不在今日可做清单")

    sectors_json = _load_sectors_json(sectors_path)
    managed_prev = _load_managed_codes(output_dir)
    data_date = primaries[0]["data_date"]

    all_rows: list[dict[str, Any]] = []
    all_leaders: list[dict[str, Any]] = []
    sector_summaries: list[dict[str, Any]] = []

    logging.info("第二层：处理 %d 个可做行业", len(primaries))
    for primary in primaries:
        rows, leaders, summary = _process_one_sector(
            output_dir, primary, sectors_json, workers=workers
        )
        all_rows.extend(rows)
        all_leaders.extend(leaders)
        sector_summaries.append(summary)

    df = pd.DataFrame(all_rows)
    out_path = output_dir / RESULT_CSV
    if not dry_run:
        df.to_csv(out_path, index=False, encoding="utf-8-sig")

    removed: list[str] = []
    added: list[str] = []
    managed_now: list[str] = []
    if apply_observation:
        removed, added, managed_now = _sync_observation(
            all_leaders,
            managed_prev,
            dry_run=dry_run,
        )

    meta = {
        "数据日期": data_date,
        "可做行业数": len(primaries),
        "行业列表": sector_summaries,
        "龙头": [{"code": r["股票代码"], "name": r["股票名称"]} for r in all_leaders],
        "observation_managed": managed_now,
        "observation_removed": removed,
        "observation_added": added,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_meta(output_dir, meta, dry_run=dry_run)

    logging.info(
        "完成 → %s | 行业=%d 龙头=%d observation -%d +%d",
        out_path if not dry_run else "(dry-run)",
        len(primaries),
        len(all_leaders),
        len(removed),
        len(added),
    )
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="申万二级战术个股（回踩可接 / 过热观望）")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--sector-code", help="指定行业代码")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-observation", action="store_true")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        run_sector_leaders(
            args.output_dir.resolve(),
            sector_code=args.sector_code,
            dry_run=args.dry_run,
            workers=max(1, args.workers),
            apply_observation=not args.no_observation,
        )
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.error("执行失败：%s", exc)
        sys.exit(1)
    except Exception:
        logging.exception("执行未预期异常")
        raise


if __name__ == "__main__":
    main()
