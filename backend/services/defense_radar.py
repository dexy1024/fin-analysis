"""
双防线「黄金伏击圈」雷达：与前端日线 A-ZD / C-ZD（按中枢时间排序后首末段下沿）一致，
结合现价扫描伏击区。不包含上证指数 sh000001。
诊断结果写入本地 Markdown（`.md`），并同步 `last_summary.json` 供 GET `/summary` 秒读；目录均为 `logs/defense_radar/`。

数据口径（默认 refresh=False，正式用法应始终如此）：
  - **假定前置任务已更新本地文件**：`services.kline_scheduler` 在 10:31/11:31/14:01/15:01 写 60m、
    16:01 另写日线；雷达**只读缓存**，不主动拉网补数。
  - C-ZD / A-ZD：本地**日线**缓存上的缠论中枢；现价 P：本地 **60m** 末根收盘（`kline_60_*.csv`）。
  - Markdown 表含 **60分钟笔向**：取 60m `pens_effective` 最后一笔方向（向上/向下）。
  - **四条件扳机（full_trigger）串联**：①伏击带 ±1% → ②末笔有效笔向下 → ③MACD（两段下跌绿柱面积缩小 **或** 末段绿柱连续缩短）→ ④合并末三根严格底分型 + K3 确认且与图 fractals 末段底分型一致；**全部为真**才记扳机（Tab 橙、前端弹窗）。
  - 仅排障时可 `refresh=True` 或命令行 `--refresh` 强制先拉线上再算。
  - 调度链内由 `kline_scheduler` 在每次 60m 同步后调用；亦可 POST `/api/diagnosis/defense-radar` 或脚本手动跑（默认仍只读本地）。
  - **梅花2test（889999）** 不列入 `DEFENSE_RADAR_WATCHLIST`：数据为 600873 基座 + 本地 mock 尾部，雷达在跑完实盘列表后 **单独追加** 一行（`analyze_meihua2test_symbol`），与生产标的隔离。
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

# 测试标的：与 production watchlist 分离；日线/60m 前半为 600873 复制，之后为 mock（见 build_meihua2test_fixture.py）
MEIHUA2TEST_CODE = "889999"
MEIHUA2TEST_NAME = "梅花2test"

# 与 frontend/src/App.tsx 中 CHART_TABS 一致（不含上证指数、不含 889999 mock）
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
    ("600585", "海螺水泥"),
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
    ("002602", "世纪华通"),
    ("688981", "中芯国际"),
    ("688041", "海光信息"),
    ("512690", "酒ETF"),
    ("hk00175", "吉利汽车"),
    ("hk03690", "美团"),
    ("hk03896", "金山云"),
    ("hk06862", "海底捞"),
    ("000538", "云南白药"),
    ("000858", "五粮液"),
    ("600938", "中国海油"),
    ("601288", "农业银行"),
    ("002475", "立讯精密"),
    ("512400", "有色金属ETF"),
    ("159985", "豆粕ETF"),
    ("159227", "航空航天ETF"),
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
            "in_c_central": r.in_c_central,
            "has_bottom_div_in_switch": r.has_bottom_div_in_switch,
            "boll_buy": r.boll_buy,
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
    except (OSError, json.JSONDecodeError, TypeError):
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
    except (OSError, TypeError):
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
    绝对防线逻辑：
    absolute_bottom = MIN(C-ZD, A-ZD)
    - 现价 >= absolute_bottom * 1.01：未跌破绝对防线（高于缓冲区）
    - absolute_bottom <= 现价 < absolute_bottom * 1.01：进入绝对防线伏击圈
    - 现价 < absolute_bottom：跌破绝对防线（破位禁买）
    """
    absolute_bottom = min(value_c, value_a)
    buffer_upper = absolute_bottom * 1.01  # 缓冲带上沿

    if p < absolute_bottom:
        return "【红色警报】已跌破绝对防线 MIN(C-ZD, A-ZD)！该标的已废，绝对禁买！"
    if absolute_bottom <= p <= buffer_upper:
        return "【一级警报】进入绝对防线伏击圈！立刻打开60分钟图盯蓝三角！"
    return "【日线】未跌破绝对防线 MIN(C-ZD, A-ZD)，等待更优入场点"


def _price_in_tier1_or_ultimate_zone(p: float, value_c: float, value_a: float) -> bool:
    """
    条件 1：现价未跌破绝对防线 MIN(C-ZD, A-ZD) 且在其 ±1% 缓冲带内（进入伏击圈）。
    """
    absolute_bottom = min(value_c, value_a)
    buffer_upper = absolute_bottom * 1.01
    # 在绝对防线之上，且在缓冲带内（不含破位）
    return bool(absolute_bottom <= p <= buffer_upper)


def chart_tail_bottom_fractal_ok(h60: Dict[str, Any]) -> bool:
    """
    与 60m 图一致：全链路 fractals 中存在底分型，且落在「最近一段」合并 K 上（与 HourlyChanChart 蓝三角同源）。

    末三根 OHLC 常与分型日期错开（底分型多在倒数第 2～4 根，末根已是反弹 K），故用末 12 根合并 K
    的日期集合与分型 date 对齐（不用 bar_index，避免与 date 不一致时误判）。
    """
    bars = h60.get("data") or []
    if len(bars) < 3:
        return False
    n = len(bars)
    n_tail = min(12, n)
    tail_from = n - n_tail
    last_keys = {_axis_date_key(b["date"]) for b in bars[tail_from:]}
    for f in h60.get("fractals") or []:
        if f.get("type") != "bottom":
            continue
        dk = f.get("date")
        if dk is None:
            continue
        try:
            if _axis_date_key(dk) in last_keys:
                return True
        except (TypeError, ValueError):
            continue
    return False


def strict_blue_triangle_last_three_raw(bars_raw: List[Dict[str, Any]]) -> bool:
    """
    雷达辅助：合并包含后取时间序末三根 K1,K2,K3，严格底分型 + K3 收盘 > K2 最低。
    四条件扳机中与 chart_tail_bottom_fractal_ok 同时满足才记为「蓝三角」通过（与图一致）。
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


def macd_green_bars_shortening_ok(bars: List[Dict[str, Any]]) -> bool:
    """
    条件 3（分支 B）：末段至少 3 根 K 的 MACD 柱均为负，且逐根抬高（绿柱缩短，向零轴收敛）。
    """
    vals: List[float] = []
    for b in bars[-12:]:
        m = b.get("macd")
        if not isinstance(m, dict):
            continue
        v = m.get("macd")
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < 3:
        return False
    t = vals[-3:]
    if not all(x < 0 for x in t):
        return False
    return bool(t[0] < t[1] < t[2])


def macd_condition3_radar_ok(h60_payload: Dict[str, Any]) -> bool:
    """
    MACD 转强判定：严格基于柱状图动能变化（导数）。
    macd_hist = (DIF - DEA) * 2，即 bars[n]["macd"]["macd"] 已经是 MACD 柱值。

    转强(True)：当前柱值 > 前一根柱值（动能向上）
      - 场景A（水下底背驰）：macd_hist < 0 且 macd_hist > prev_macd_hist（绿柱缩短）
      - 场景B（水上主升浪）：macd_hist > 0 且 macd_hist > prev_macd_hist（红柱伸长）
    转弱(False)：当前柱值 < 前一根柱值（动能向下）
      - 场景C（水下主跌浪）：macd_hist < 0 且 macd_hist < prev_macd_hist（绿柱伸长）
      - 场景D（水上顶背驰）：macd_hist > 0 且 macd_hist < prev_macd_hist（红柱缩短）
    """
    bars = h60_payload.get("data") or []
    if len(bars) < 2:
        return False
    m0_obj = bars[-1].get("macd")
    m1_obj = bars[-2].get("macd")
    if not isinstance(m0_obj, dict) or not isinstance(m1_obj, dict):
        return False
    m0 = m0_obj.get("macd")
    m1 = m1_obj.get("macd")
    if m0 is None or m1 is None:
        return False
    try:
        m0v = float(m0)
        m1v = float(m1)
    except (TypeError, ValueError):
        return False
    return m0v > m1v


def macd_momentum_ok_two_down_pens(h60_payload: Dict[str, Any]) -> bool:
    """兼容单元测试与旧调用名；等价于 macd_condition3_radar_ok。"""
    return macd_condition3_radar_ok(h60_payload)


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
    macd_momentum_ok: bool
    blue_triangle_strict: bool
    full_trigger: bool
    # 60分钟买点7条件完整字段（与前端对齐）
    in_c_central: bool
    has_bottom_div_in_switch: bool
    boll_buy: bool


def _append_meihua2test_row_if_missing(rows: List[DefenseRow], *, refresh: bool) -> None:
    """889999 不混入 production 循环，仅在末尾单独追加（若调用方未已包含）。"""
    if any(r.code == MEIHUA2TEST_CODE for r in rows):
        return
    rows.append(analyze_meihua2test_symbol(refresh=refresh))


def _get_full_radar_watchlist() -> Tuple[Tuple[str, str], ...]:
    """DEFENSE_RADAR_WATCHLIST + observation.json 中的标的（去重，observation 排在后面）。"""
    obs = _load_watchlist_observation_symbols()
    seen = set(code for code, _ in DEFENSE_RADAR_WATCHLIST)
    extra = [(code, name) for code, name in obs if code not in seen]
    return DEFENSE_RADAR_WATCHLIST + tuple(extra)


def build_defense_radar_summary(
    *,
    refresh: bool = False,
    watchlist: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> List[DefenseRadarSummaryItem]:
    wl = watchlist or _get_full_radar_watchlist()
    rows: List[DefenseRow] = []
    for code, name in wl:
        rows.append(analyze_symbol(code, name, refresh=refresh))
    _append_meihua2test_row_if_missing(rows, refresh=refresh)
    return defense_rows_to_summary_items(rows)


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


def _macd_neg_area(bars: List[Dict[str, Any]], d0: str, d1: str) -> float:
    """计算指定日期范围内的MACD负值区域面积（绿柱面积）。"""
    total = 0.0
    for bar in bars:
        date = bar.get("date")
        if date < d0:
            continue
        if date > d1:
            break
        macd = bar.get("macd", {}).get("macd")
        if macd is not None and isinstance(macd, (int, float)) and macd < 0:
            total += abs(macd)
    return total


def _compute_bottom_divergence_arrows(
    bars: List[Dict[str, Any]], pens_eff: List[Dict[str, Any]]
) -> List[Tuple[str, float]]:
    """
    计算底背驰箭头位置（与前端 divergenceArrowPointsFromDownPens 对齐）。
    相邻两根向下笔：终点创新低，且绿柱面积缩小或笔长度更短 → 底背驰。
    返回: [(date, y), ...]
    """
    downs = [p for p in pens_eff if p.get("direction") == "down"]
    out: List[Tuple[str, float]] = []
    for i in range(1, len(downs)):
        prev = downs[i - 1]
        last = downs[i]
        # 终点必须创新低
        if last.get("end_price", 0) >= prev.get("end_price", 0):
            continue
        # 计算绿柱面积
        area_prev = _macd_neg_area(bars, prev.get("start_date", ""), prev.get("end_date", ""))
        area_last = _macd_neg_area(bars, last.get("start_date", ""), last.get("end_date", ""))
        # 计算笔长度
        len_prev = abs(prev.get("end_price", 0) - prev.get("start_price", 0))
        len_last = abs(last.get("end_price", 0) - last.get("start_price", 0))
        # 判断背驰（面积缩小或笔长更短）
        weaker = len_last < len_prev or (area_prev > 1e-8 and area_last < area_prev)
        if not weaker:
            continue
        # 找到对应K线的low值
        y = last.get("end_price", 0)
        for bar in bars:
            if bar.get("date") == last.get("end_date"):
                y = bar.get("low", y)
                break
        out.append((last.get("end_date", ""), y))
    return out


def _compute_hourly_buy_conditions(
    h60: Dict[str, Any], bars: List[Dict[str, Any]], last_price: float
) -> Tuple[bool, bool, bool]:
    """
    计算60分钟买点7条件中的剩余3个（与前端 computeHourlyBuySellState 对齐）：
    - in_c_central: 现价在C中枢内（ZD～ZG）
    - has_bottom_div_in_switch: 底背驰点落在当前向上笔内
    - boll_buy: BOLL站回中轨
    返回: (in_c_central, has_bottom_div_in_switch, boll_buy)
    """
    # 1. in_c_central: 检查现价是否在60分钟C中枢内（ZD～ZG）
    centrals_raw = h60.get("centrals") or []
    in_c_central = False
    if centrals_raw:
        # 按时间排序，最后一个中枢是C中枢
        centrals_sorted = sorted(
            list(centrals_raw),
            key=lambda c: (c.get("start_date", ""), c.get("end_date", ""))
        )
        c_central = centrals_sorted[-1] if centrals_sorted else None
        if c_central:
            c_zd = float(c_central.get("zd") or 0)
            c_zg = float(c_central.get("zg") or 0)
            if c_zd and c_zg:
                in_c_central = c_zd <= last_price <= c_zg

    # 2. has_bottom_div_in_switch: 底背驰点是否在当前向上笔内
    has_bottom_div_in_switch = False
    pens_eff = h60.get("pens_effective") or []
    if len(pens_eff) >= 2:
        # 最后两笔：前一下笔、当前上笔
        prev_pen = pens_eff[-2]
        curr_pen = pens_eff[-1]
        if prev_pen.get("direction") == "down" and curr_pen.get("direction") == "up":
            # 实时计算底背驰箭头（不再依赖不存在的 divergence_arrows_down 字段）
            div_arrows = _compute_bottom_divergence_arrows(bars, pens_eff)
            if div_arrows:
                # 获取最后一个底背驰箭头
                last_div_date, _ = div_arrows[-1]
                # 检查背驰点是否在向上笔的时间范围内
                pen_start = curr_pen.get("start_date")
                pen_end = curr_pen.get("end_date")
                if pen_start and pen_end:
                    has_bottom_div_in_switch = pen_start <= last_div_date <= pen_end

    # 3. boll_buy: BOLL站回中轨（与前端逻辑对齐）
    boll_buy = False
    if len(bars) >= 2:
        last_bar = bars[-1]
        prev_bar = bars[-2]
        last_boll = last_bar.get("boll") or {}
        prev_boll = prev_bar.get("boll") or {}
        if last_boll.get("middle") and prev_boll.get("middle"):
            last_close = float(last_bar.get("close") or 0)
            prev_close = float(prev_bar.get("close") or 0)
            last_middle = float(last_boll.get("middle") or 0)
            prev_middle = float(prev_boll.get("middle") or 0)
            last_lower = float(last_boll.get("lower") or 0)
            prev_lower = float(prev_boll.get("lower") or 0)
            # 站回中轨：当前收盘 > 中轨，且（前收盘 <= 前中轨 或 前低 <= 前下轨*1.01）
            boll_buy = (
                last_close > last_middle
                and (prev_close <= prev_middle or (prev_bar.get("low") and float(prev_bar.get("low")) <= prev_lower * 1.01))
            )

    return in_c_central, has_bottom_div_in_switch, boll_buy


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
    macd_momentum_ok: bool = False
    blue_triangle_strict: bool = False
    full_trigger: bool = False
    # 60分钟买点7条件中的剩余3个（与前端HourlyBuyConditionFlags对齐）
    in_c_central: bool = False  # 【60m】现价在C中枢内（ZD～ZG）
    has_bottom_div_in_switch: bool = False  # 【60m】底背驰点落在当前向上笔内
    boll_buy: bool = False  # 【60m】BOLL站回中轨


def analyze_meihua2test_symbol(*, refresh: bool = False) -> DefenseRow:
    """
    梅花2test（889999）专用入口：与 `DEFENSE_RADAR_WATCHLIST` 中的实盘标的隔离。
    数据由 `build_meihua2test_fixture.py` 生成（600873 历史 + 日历未来 mock）；`get_index_kline` 对 889999
    在 `MEIHUA2TEST_FUTURE_K=1` 时放宽 end_ts，否则未来 K 不参与计算。
    """
    return _compute_defense_row(MEIHUA2TEST_CODE, MEIHUA2TEST_NAME, refresh=refresh)


def analyze_symbol(code: str, name: str, *, refresh: bool = False) -> DefenseRow:
    """production 标的；889999 请用 `analyze_meihua2test_symbol`（本函数对 889999 仍转发至该入口）。"""
    if code.strip() == MEIHUA2TEST_CODE:
        return analyze_meihua2test_symbol(refresh=refresh)
    return _compute_defense_row(code.strip(), name, refresh=refresh)


def _compute_defense_row(code: str, name: str, *, refresh: bool = False) -> DefenseRow:
    """四条件雷达核心计算（889999 与实盘共用本实现，仅入口与 watchlist 分离）。"""
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
        daily_start = (datetime.now() - timedelta(days=380)).strftime("%Y-%m-%d")
        payload = get_index_kline(
            symbol=code.strip(),
            start_date=daily_start,
            end_date=None,
            period="daily",
            refresh=refresh,
        )
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
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
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
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

    # ========== 7个买点条件（与前端 HourlyBuyConditionFlags 对齐）==========
    # 1. radar_zone_ok（原：±1%缓冲带）-> 改为：keepDailySupport（现价 >= MIN(C-ZD, A-ZD)）
    absolute_bottom = min(c_zd, a_zd) if c_zd and a_zd else None
    radar_zone_ok = absolute_bottom is not None and p >= absolute_bottom

    # 2. pen_60m_down（原：末笔向下）-> 改为：switchedDownToUp（前一下笔、当前上笔）
    pens_eff = h60.get("pens_effective") or []
    switched_down_to_up = (
        len(pens_eff) >= 2
        and pens_eff[-2].get("direction") == "down"
        and pens_eff[-1].get("direction") == "up"
    )
    pen_60m_down = switched_down_to_up  # 字段名保留，逻辑改为「前下+当前上」
    pen_label = _effective_60m_pen_label(h60)

    # 3. macd_momentum_ok：MACD 转强判定
    macd_ok = macd_condition3_radar_ok(h60)
    if code == "603317":
        m0 = bars[-1].get("macd", {}).get("macd") if bars else None
        m1 = bars[-2].get("macd", {}).get("macd") if len(bars) >= 2 else None
        print(f"[DEBUG 603317] m0={m0}, m1={m1}, macd_ok={macd_ok}", flush=True)

    # 4. blue_triangle_strict（原：末三K底分型）-> 改为：hasBottomFractalInSwitch（当前向上笔内有底分型）
    fractals = h60.get("fractals") or []
    last_up_pen = pens_eff[-1] if switched_down_to_up else None
    blue_ok = False
    if last_up_pen:
        blue_ok = any(
            f.get("type") == "bottom"
            and last_up_pen.get("start_date") <= f.get("date") <= last_up_pen.get("end_date")
            for f in fractals
        )

    # 5-7. 其余3个条件
    in_c_central, has_bottom_div_in_switch, boll_buy = _compute_hourly_buy_conditions(h60, bars, p)

    # 四条件串联过滤（全部为真才 full_trigger / Tab 橙色）——保留原口径用于触发信号
    zone_ok_legacy = _price_in_tier1_or_ultimate_zone(p, c_zd, a_zd)
    pen_down_legacy = pen_label == "向下"
    strict_tri_legacy = strict_blue_triangle_last_three_raw(bars)
    chart_bottom_legacy = chart_tail_bottom_fractal_ok(h60)
    blue_ok_legacy = bool(strict_tri_legacy and chart_bottom_legacy)
    full_ok = bool(zone_ok_legacy and pen_down_legacy and macd_ok and blue_ok_legacy)

    return DefenseRow(
        code=code,
        name=name,
        alert=alert,
        c_zd=round(c_zd, 4),
        a_zd=round(a_zd, 4),
        last_price=round(p, 4),
        error=None,
        pen_60m=pen_label,
        radar_zone_ok=radar_zone_ok,
        pen_60m_down=pen_60m_down,
        macd_momentum_ok=macd_ok,
        blue_triangle_strict=blue_ok,
        full_trigger=full_ok,
        in_c_central=in_c_central,
        has_bottom_div_in_switch=has_bottom_div_in_switch,
        boll_buy=boll_buy,
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

    wl = watchlist or _get_full_radar_watchlist()
    rows_out: List[DefenseRow] = []
    for code, name in wl:
        rows_out.append(analyze_symbol(code, name, refresh=refresh))
    _append_meihua2test_row_if_missing(rows_out, refresh=refresh)

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
    except (OSError, TypeError):
        logging.exception("defense_radar: 写入 %s 失败", LAST_SUMMARY_JSON)

    logging.info("defense_radar: 已写入 %s（共 %s 行）", path, len(rows_out))
    return path


# ==================== 破位状态批量计算（供定时调度调用） ====================

BROKEN_SYMBOLS_JSON = "broken_symbols.json"


def _load_watchlist_observation_symbols() -> List[Tuple[str, str]]:
    """读取 watchlist.json 和 observation.json，返回 (code, name) 列表。"""
    symbols: List[Tuple[str, str]] = []
    root = Path(__file__).resolve().parents[2]

    watchlist_path = root / "backend" / "data" / "watchlist.json"
    if watchlist_path.is_file():
        try:
            data = json.loads(watchlist_path.read_text(encoding="utf-8"))
            for item in data.get("holdings", []):
                if isinstance(item, dict) and item.get("code"):
                    symbols.append((str(item["code"]).strip(), str(item.get("name", "")).strip()))
        except (OSError, json.JSONDecodeError, TypeError):
            logging.warning("defense_radar: 读取 watchlist.json 失败")

    observation_path = root / "backend" / "data" / "observation.json"
    if observation_path.is_file():
        try:
            data = json.loads(observation_path.read_text(encoding="utf-8"))
            for item in data.get("observations", []):
                if isinstance(item, dict) and item.get("code"):
                    code = str(item["code"]).strip()
                    name = str(item.get("name", "")).strip()
                    # 去重：已存在于 watchlist 的跳过
                    if not any(c == code for c, _ in symbols):
                        symbols.append((code, name))
        except (OSError, json.JSONDecodeError, TypeError):
            logging.warning("defense_radar: 读取 observation.json 失败")

    return symbols


def _is_symbol_broken(code: str) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """
    判断单个标的是否破位。
    返回: (is_broken, a_zd, c_zd, last_price)
    破位定义：60分钟最新收盘价 < MIN(日线A-ZD, 日线C-ZD)
    """
    sym = code.strip()
    if sym in EXCLUDED_SYMBOLS or sym.lower() in EXCLUDED_SYMBOLS:
        return False, None, None, None

    # 1. 获取日线数据（取 centrals）
    try:
        daily_start = (datetime.now() - timedelta(days=380)).strftime("%Y-%m-%d")
        daily = get_index_kline(
            symbol=sym,
            start_date=daily_start,
            end_date=None,
            period="daily",
            refresh=False,
        )
    except (ValueError, OSError, TypeError, KeyError, RuntimeError):
        logging.warning("defense_radar: 日线数据获取失败 %s", sym)
        return False, None, None, None

    centrals_raw = daily.get("centrals") or []
    centrals = _sort_centrals_chronologically(list(centrals_raw))
    a_zd, c_zd = _daily_a_c_zd(centrals)
    if a_zd is None or c_zd is None:
        return False, None, None, None

    # 2. 获取60分钟最新收盘价
    try:
        h60 = get_index_kline(
            symbol=sym,
            start_date=_h60_start_date(90),
            end_date=None,
            period="60",
            refresh=False,
        )
    except (ValueError, OSError, TypeError, KeyError, RuntimeError):
        logging.warning("defense_radar: 60分钟数据获取失败 %s", sym)
        return False, a_zd, c_zd, None

    bars = h60.get("data") or []
    if not bars:
        return False, a_zd, c_zd, None

    last_price = float(bars[-1]["close"])
    min_zd = min(a_zd, c_zd)
    is_broken = last_price < min_zd

    return is_broken, a_zd, c_zd, last_price


def compute_and_save_broken_symbols() -> Path:
    """
    计算 watchlist + observation 中所有标的的破位状态，保存到 broken_symbols.json。
    由 kline_scheduler 在每次定时调度完成后调用。
    """
    symbols = _load_watchlist_observation_symbols()
    if not symbols:
        logging.info("defense_radar: watchlist 和 observation 均为空，跳过破位计算")
        out_dir = radar_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / BROKEN_SYMBOLS_JSON
        path.write_text(
            json.dumps({"generated_at": datetime.now().replace(microsecond=0).isoformat(), "broken_codes": []}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    broken_codes: List[str] = []
    details: List[Dict[str, Any]] = []

    for code, name in symbols:
        is_broken, a_zd, c_zd, last_price = _is_symbol_broken(code)
        if is_broken:
            broken_codes.append(code)
        details.append({
            "code": code,
            "name": name,
            "is_broken": is_broken,
            "a_zd": round(a_zd, 4) if a_zd is not None else None,
            "c_zd": round(c_zd, 4) if c_zd is not None else None,
            "last_price": round(last_price, 4) if last_price is not None else None,
        })

    payload: Dict[str, Any] = {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "broken_codes": broken_codes,
        "details": details,
    }

    out_dir = radar_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / BROKEN_SYMBOLS_JSON
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    logging.info("defense_radar: 破位状态已写入 %s（%d 个标的，%d 个破位）", path, len(symbols), len(broken_codes))
    return path


def load_broken_symbols_json(radar_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """读取 broken_symbols.json，供 API 接口使用。"""
    d = radar_dir or radar_output_dir()
    path = d / BROKEN_SYMBOLS_JSON
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        logging.warning("defense_radar: 读取 %s 失败", path)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run_defense_radar()
    print(out)
