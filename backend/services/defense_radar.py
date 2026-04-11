"""
双防线「黄金伏击圈」雷达：与前端日线 A-ZD / C-ZD（按中枢时间排序后首末段下沿）一致，
结合现价扫描伏击区。不包含上证指数 sh000001。
诊断结果写入本地 Markdown（`.md`），并同步 `last_summary.json` 供 GET `/summary` 秒读；目录均为 `logs/defense_radar/`。

数据口径（默认 refresh=False，正式用法应始终如此）：
  - **假定前置任务已更新本地文件**：`services.kline_scheduler` 在 10:31/11:31/14:01/15:01 写 60m、
    16:01 另写日线；雷达**只读缓存**，不主动拉网补数。
  - C-ZD / A-ZD：本地**日线**缓存上的缠论中枢；现价 P：本地 **60m** 末根收盘（`kline_60_*.csv`）。
  - Markdown 表含 **60分钟笔向**：取 60m `pens_effective` 最后一笔方向（向上/向下）。
  - 仅排障时可 `refresh=True` 或命令行 `--refresh` 强制先拉线上再算。
  - 调度链内由 `kline_scheduler` 在每次 60m 同步后调用；亦可 POST `/api/diagnosis/defense-radar` 或脚本手动跑（默认仍只读本地）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from services.indicators import _axis_date_key, _merge_inclusive_bars, get_index_kline

# 条件 3：比较最近两段向下有效笔的 MACD 绿柱（零轴下）面积；设 false 则摘要中 macd_momentum_ok 恒为 null
RADAR_MACD_FILTER_ENABLED: bool = os.environ.get("RADAR_MACD_FILTER", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# 与 frontend/src/App.tsx 中 CHART_TABS 一致（不含上证指数；本列表为雷达扫描范围）
DEFENSE_RADAR_WATCHLIST: Tuple[Tuple[str, str], ...] = (
    ("510300", "沪深300ETF"),
    ("159915", "创业板ETF"),
    ("588000", "科创50ETF"),
    ("588200", "科创芯片ETF"),
    ("159755", "电池ETF"),
    ("513130", "恒生科技ETF"),
    ("159992", "创新药ETF"),
    ("515790", "光伏ETF"),
    ("159899", "软件ETF"),
    ("513360", "教育ETF"),
    ("601225", "陕西煤业"),
    ("002508", "老板电器"),
    ("000333", "美的集团"),
    ("000429", "粤高速"),
    ("000423", "东阿阿胶"),
    ("000338", "潍柴动力"),
    ("000895", "双汇发展"),
    ("600011", "华能国际"),
    ("601138", "工业富联"),
    ("600660", "福耀玻璃"),
    ("300048", "合康新能"),
    ("002415", "海康威视"),
    ("601919", "中远海控"),
    ("600873", "梅花生物"),
    ("601166", "兴业银行"),
    ("600900", "长江电力"),
    ("600887", "伊利股份"),
    ("603317", "天味食品"),
    ("601728", "中国电信"),
    ("601857", "中国石油"),
    ("601766", "中国中车"),
    ("600096", "云天化"),
    ("000001", "平安银行"),
    ("000651", "格力电器"),
    ("002230", "科大讯飞"),
    ("002714", "牧原股份"),
    ("hk01810", "小米集团"),
)

EXCLUDED_SYMBOLS = frozenset({"sh000001", "SH000001"})

LAST_SUMMARY_JSON = "last_summary.json"


def radar_output_dir(root: Optional[Path] = None) -> Path:
    base = root or Path(__file__).resolve().parents[2]
    return base / "logs" / "defense_radar"


def defense_rows_to_summary_items(rows: List[DefenseRow]) -> List[DefenseRadarSummaryItem]:
    return [
        {
            "code": r.code,
            "name": r.name,
            "alert": r.alert,
            "has_alert": defense_alert_is_active(r.alert),
            "pen_60m": r.pen_60m or "",
            "radar_zone_ok": r.radar_zone_ok,
            "pen_60m_down": r.pen_60m_down,
            "macd_momentum_ok": r.macd_momentum_ok,
            "blue_triangle_strict": r.blue_triangle_strict,
            "full_trigger": r.full_trigger,
        }
        for r in rows
    ]


def load_last_summary_json(radar_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    d = radar_dir or radar_output_dir()
    path = d / LAST_SUMMARY_JSON
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logging.warning("defense_radar: 读取 %s 失败", path)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        return None
    return data


def write_last_summary_json(out_dir: Path, rows_out: List[DefenseRow], generated_at_iso: str) -> Path:
    payload: Dict[str, Any] = {
        "generated_at": generated_at_iso,
        "symbols": defense_rows_to_summary_items(rows_out),
    }
    path = out_dir / LAST_SUMMARY_JSON
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def get_defense_radar_summary_for_api(*, refresh: bool = False) -> Dict[str, Any]:
    """
    供 GET /summary：优先读 last_summary.json（与最近一次雷达 md 同步），无缓存时再现场计算并回写 json。
    """
    if not refresh:
        cached = load_last_summary_json()
        if cached is not None:
            return cached
    symbols = build_defense_radar_summary(refresh=refresh)
    now_iso = datetime.now().replace(microsecond=0).isoformat()
    payload: Dict[str, Any] = {"generated_at": now_iso, "symbols": symbols}
    try:
        out_dir = radar_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / LAST_SUMMARY_JSON
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        logging.exception("defense_radar: 写入 %s 失败", LAST_SUMMARY_JSON)
    return payload


def _md_cell(v: object) -> str:
    """Markdown 表格单元格：转义竖线并压成单行。"""
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _h60_start_date(days_ago: int = 90) -> str:
    """与 frontend startDateDaysAgo(90) 对齐，保证与 60m 图请求区间一致。"""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _sort_centrals_chronologically(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """与 App.tsx sortCentralsChronologically 一致：按 start_date、再 end_date 升序。"""
    return sorted(
        raw,
        key=lambda c: (str(c.get("start_date", "")), str(c.get("end_date", ""))),
    )


def _daily_a_c_zd(centrals_sorted: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """A-ZD = 时间轴上第一个中枢下沿；C-ZD = 最后一个中枢下沿。"""
    if not centrals_sorted:
        return None, None
    a_zd = float(centrals_sorted[0]["zd"])
    c_zd = float(centrals_sorted[-1]["zd"])
    return a_zd, c_zd


def _classify(
    p: float,
    value_c: float,
    value_a: float,
) -> str:
    """
    与用户伪代码一致：
    Support_High = MAX(C-ZD, A-ZD), Support_Low = MIN(...)；
    伏击圈为各自 ±1%。
    """
    support_high = max(value_c, value_a)
    support_low = min(value_c, value_a)
    z1_upper = support_high * 1.01
    z1_lower = support_high * 0.99
    z2_upper = support_low * 1.01
    z2_lower = support_low * 0.99

    if z1_lower <= p <= z1_upper:
        return "【一级警报】进入第一防线伏击圈！立刻打开60分钟图盯蓝三角！"
    if p < z1_lower and p > z2_upper:
        return "观望，等待飞刀继续下落..."
    if z2_lower <= p <= z2_upper:
        return "【终极警报】退守极限防线！进入伏击圈！盯60分钟蓝三角！"
    if p < z2_lower:
        return "【红色警报】双防线严重破位！该标的已废，绝对禁买！"
    if p > z1_upper:
        return "现价高于第一防线伏击上沿，未进入伏击区"
    return "其他区间（请人工核对价位与中枢）"


def _price_in_tier1_or_ultimate_zone(p: float, value_c: float, value_a: float) -> bool:
    """
    条件 1：现价落在第一防线 ±1% 或极限防线 ±1%（不含观望带、不含红色破位、不含高于上沿）。
    """
    support_high = max(value_c, value_a)
    support_low = min(value_c, value_a)
    z1_upper = support_high * 1.01
    z1_lower = support_high * 0.99
    z2_upper = support_low * 1.01
    z2_lower = support_low * 0.99
    in_z1 = z1_lower <= p <= z1_upper
    in_z2 = z2_lower <= p <= z2_upper
    return bool(in_z1 or in_z2)


def strict_blue_triangle_last_three_raw(bars_raw: List[Dict[str, Any]]) -> bool:
    """
    仅雷达：合并包含后取时间序末三根 K1,K2,K3，严格底分型 + K3 收盘 > K2 最低。
    与全链路 _find_fractals_from_standardized 无关，不改变图表分型。
    """
    if len(bars_raw) < 3:
        return False
    source: List[Dict[str, Any]] = []
    for b in bars_raw:
        try:
            source.append(
                {
                    "date": b["date"],
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b.get("volume") or 0),
                },
            )
        except (KeyError, TypeError, ValueError):
            return False
    std = _merge_inclusive_bars(source)
    if len(std) < 3:
        return False
    k1, k2, k3 = std[-3], std[-2], std[-1]
    lh, ll = float(k1["high"]), float(k1["low"])
    mh, ml = float(k2["high"]), float(k2["low"])
    rh, rl = float(k3["high"]), float(k3["low"])
    c3 = float(k3["close"])
    strict_low = ml < ll and ml < rl
    strict_high = mh < lh and mh < rh
    confirm = c3 > ml
    return bool(strict_low and strict_high and confirm)


def _macd_neg_green_area_between(bars: List[Dict[str, Any]], d_start: str, d_end: str) -> float:
    """笔区间内仅统计 MACD 柱 < 0 的部分（绿柱），力度用绝对值之和近似。"""
    k0 = _axis_date_key(d_start)
    k1 = _axis_date_key(d_end)
    s = 0.0
    for b in bars:
        ds = _axis_date_key(b["date"])
        if ds < k0:
            continue
        if ds > k1:
            break
        macd_obj = b.get("macd")
        if not isinstance(macd_obj, dict):
            continue
        v = macd_obj.get("macd")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < 0:
            s += abs(fv)
    return s


def macd_momentum_ok_two_down_pens(h60_payload: Dict[str, Any]) -> Optional[bool]:
    """
    条件 3：最近一段向下有效笔的绿柱面积 < 再上一段向下有效笔（面积缩小则 True）。
    未启用 MACD 过滤时返回 None；有效笔不足两段向下笔时返回 True（不挡扳机）。
    """
    if not RADAR_MACD_FILTER_ENABLED:
        return None
    pens = h60_payload.get("pens_effective") or []
    if not pens:
        return True
    down_pens: List[Dict[str, Any]] = [p for p in pens if p.get("direction") == "down"]
    if len(down_pens) < 2:
        return True
    prev_pen = down_pens[-2]
    cur_pen = down_pens[-1]
    bars = h60_payload.get("data") or []
    if not bars:
        return True
    a_prev = _macd_neg_green_area_between(bars, str(prev_pen["start_date"]), str(prev_pen["end_date"]))
    a_cur = _macd_neg_green_area_between(bars, str(cur_pen["start_date"]), str(cur_pen["end_date"]))
    if a_prev <= 1e-12:
        return True
    return bool(a_cur < a_prev)


# 与前端「有警报才展示 tab」一致：仅这三种算有效警报（含【】前缀）
RADAR_ALERT_MARKERS: Tuple[str, ...] = ("【一级警报】", "【终极警报】", "【红色警报】")


def defense_alert_is_active(alert: str) -> bool:
    return any(m in alert for m in RADAR_ALERT_MARKERS)


class DefenseRadarSummaryItem(TypedDict):
    code: str
    name: str
    alert: str
    has_alert: bool
    pen_60m: str
    radar_zone_ok: bool
    pen_60m_down: bool
    macd_momentum_ok: Optional[bool]
    blue_triangle_strict: bool
    full_trigger: bool


def build_defense_radar_summary(
    *,
    refresh: bool = False,
    watchlist: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> List[DefenseRadarSummaryItem]:
    wl = watchlist or DEFENSE_RADAR_WATCHLIST
    out: List[DefenseRadarSummaryItem] = []
    for code, name in wl:
        row = analyze_symbol(code, name, refresh=refresh)
        out.append(
            {
                "code": row.code,
                "name": row.name,
                "alert": row.alert,
                "has_alert": defense_alert_is_active(row.alert),
                "pen_60m": row.pen_60m or "",
                "radar_zone_ok": row.radar_zone_ok,
                "pen_60m_down": row.pen_60m_down,
                "macd_momentum_ok": row.macd_momentum_ok,
                "blue_triangle_strict": row.blue_triangle_strict,
                "full_trigger": row.full_trigger,
            },
        )
    return out


def _effective_60m_pen_label(h60_payload: Dict[str, Any]) -> Optional[str]:
    """与前端 HourlyChanChart 一致：有效笔序列最后一笔 → 向上 / 向下。"""
    pe = h60_payload.get("pens_effective") or []
    if not pe:
        return None
    d = pe[-1].get("direction")
    if d == "up":
        return "向上"
    if d == "down":
        return "向下"
    return None


@dataclass
class DefenseRow:
    code: str
    name: str
    alert: str
    c_zd: Optional[float]
    a_zd: Optional[float]
    last_price: Optional[float]
    error: Optional[str] = None
    pen_60m: Optional[str] = None
    radar_zone_ok: bool = False
    pen_60m_down: bool = False
    macd_momentum_ok: Optional[bool] = None
    blue_triangle_strict: bool = False
    full_trigger: bool = False


def analyze_symbol(code: str, name: str, *, refresh: bool = False) -> DefenseRow:
    """日线缓存取 A/C 中枢 ZD；现价取本地 60 分钟最后一根收盘（与定时同步的 60m 数据一致）。"""
    if code.strip() in EXCLUDED_SYMBOLS or code.lower() in EXCLUDED_SYMBOLS:
        return DefenseRow(
            code=code,
            name=name,
            alert="已跳过（上证指数不参与本雷达）",
            c_zd=None,
            a_zd=None,
            last_price=None,
            error="skipped_index",
        )
    try:
        payload = get_index_kline(
            symbol=code.strip(),
            start_date="2024-12-01",
            end_date=None,
            period="daily",
            refresh=refresh,
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("defense_radar: 拉取日线失败 %s", code)
        return DefenseRow(
            code=code,
            name=name,
            alert=f"数据异常：{exc}",
            c_zd=None,
            a_zd=None,
            last_price=None,
            error=str(exc),
        )

    centrals_raw = payload.get("centrals") or []
    centrals = _sort_centrals_chronologically(list(centrals_raw))
    a_zd, c_zd = _daily_a_c_zd(centrals)
    if a_zd is None or c_zd is None:
        return DefenseRow(
            code=code,
            name=name,
            alert="无法计算：日线未形成可用中枢（或无 C-ZD/A-ZD）",
            c_zd=None,
            a_zd=None,
            last_price=None,
            error="no_central",
        )

    sym = code.strip()
    try:
        h60 = get_index_kline(
            symbol=sym,
            start_date=_h60_start_date(90),
            end_date=None,
            period="60",
            refresh=refresh,
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("defense_radar: 读取60分钟失败 %s", code)
        return DefenseRow(
            code=code,
            name=name,
            alert=f"无法计算现价：{exc}（需先完成60分钟定时同步，生成本地 kline_60 缓存）",
            c_zd=round(c_zd, 4),
            a_zd=round(a_zd, 4),
            last_price=None,
            error=str(exc),
        )

    bars = h60.get("data") or []
    if not bars:
        return DefenseRow(
            code=code,
            name=name,
            alert="无法计算：本地60分钟K线为空",
            c_zd=round(c_zd, 4),
            a_zd=round(a_zd, 4),
            last_price=None,
            error="no_60m_bars",
        )

    p = float(bars[-1]["close"])
    alert = _classify(p, c_zd, a_zd)
    zone_ok = _price_in_tier1_or_ultimate_zone(p, c_zd, a_zd)
    pen_label = _effective_60m_pen_label(h60)
    pen_down = pen_label == "向下"
    macd_ok = macd_momentum_ok_two_down_pens(h60)
    blue_ok = strict_blue_triangle_last_three_raw(bars)
    macd_pass = macd_ok is None or macd_ok is True
    full_ok = bool(zone_ok and pen_down and blue_ok and macd_pass)
    return DefenseRow(
        code=code,
        name=name,
        alert=alert,
        c_zd=round(c_zd, 4),
        a_zd=round(a_zd, 4),
        last_price=round(p, 4),
        error=None,
        pen_60m=pen_label,
        radar_zone_ok=zone_ok,
        pen_60m_down=pen_down,
        macd_momentum_ok=macd_ok,
        blue_triangle_strict=blue_ok,
        full_trigger=full_ok,
    )


def run_defense_radar(
    *,
    refresh: bool = False,
    output_dir: Optional[Path] = None,
    watchlist: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> Path:
    """
    扫描 watchlist，写出 Markdown 表格文件。
    默认目录：项目根下 logs/defense_radar/，文件名 defense_radar_YYYYMMDD_HHMMSS.md
    默认 refresh=False：日线 + 60 分钟均只读本地缓存（现价取自 60m；依赖定时任务已先同步）。
    """
    root = Path(__file__).resolve().parents[2]
    out_dir = output_dir or (root / "logs" / "defense_radar")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    display_time = now.strftime("%Y-%m-%d %H:%M:%S")
    path = out_dir / f"defense_radar_{ts}.md"

    wl = watchlist or DEFENSE_RADAR_WATCHLIST
    rows_out: List[DefenseRow] = []
    for code, name in wl:
        rows_out.append(analyze_symbol(code, name, refresh=refresh))

    lines: List[str] = [
        "# 双防线雷达",
        "",
        f"生成时间：`{display_time}`",
        "",
        "| 代码 | 标的名称 | 预警信息 | C-ZD价格 | A-ZD价格 | 现价(60m末根收盘) | 60分钟笔向 | 四条件扳机 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows_out:
        cz = "" if r.c_zd is None else f"{r.c_zd:.4f}"
        az = "" if r.a_zd is None else f"{r.a_zd:.4f}"
        lp = "" if r.last_price is None else f"{r.last_price:.4f}"
        pen = r.pen_60m or ""
        trig = "是" if r.full_trigger else "否"
        lines.append(
            f"| {_md_cell(r.code)} | {_md_cell(r.name)} | {_md_cell(r.alert)} | "
            f"{_md_cell(cz)} | {_md_cell(az)} | {_md_cell(lp)} | {_md_cell(pen)} | {_md_cell(trig)} |",
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    gen_iso = datetime.now().replace(microsecond=0).isoformat()
    try:
        write_last_summary_json(out_dir, rows_out, gen_iso)
    except Exception:  # noqa: BLE001
        logging.exception("defense_radar: 写入 %s 失败", LAST_SUMMARY_JSON)

    logging.info("defense_radar: 已写入 %s（共 %s 行）", path, len(rows_out))
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run_defense_radar()
    print(out)
