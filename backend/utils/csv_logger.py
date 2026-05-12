"""
15分钟级状态机快照日志模块（CSV 中文版）

用途：
- 在每次15分钟巡检结束、状态机计算完毕后，将所有标的的状态追加写入 CSV。
- 沪深300批量导出：logs/snapshots_hs300_YYYY.csv（表头与同 snapshots_YYYY.csv）。
- 支持 Excel 直接打开（utf-8-sig BOM 头）。
- 自选/观测：按年分文件 logs/snapshots_YYYY.csv；若环境变量 `FIN_SNAPSHOT_CSV_SUFFIX=_new` 则为 `snapshots_YYYY_new.csv`（供脚本与旧文件分离）。
- 若首行为历史「18 列」表头（缺 60m交易 / 区间价格对齐）：**原地迁移**为当前列定义并保留全部历史行（新列填空），再写入本轮首行；后续标的仍追加。
- 其它表头与程序不一致：**绝不**移动或清空现有文件，直接报错 `SnapshotCsvHeaderConflictError`，请用户自行备份/修正后再跑。
- 本模块不由 kline_scheduler 调用；快照由 run_trade_command.py / generate_snapshots.sh（或外部定时任务）触发。
"""

from __future__ import annotations

import copy
import csv
import fcntl
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

# 项目根目录（backend/utils/ 的上两级）
ROOT_DIR = Path(__file__).resolve().parents[2]
LOGS_DIR = ROOT_DIR / "logs"


class SnapshotCsvHeaderConflictError(ValueError):
    """磁盘上 snapshots CSV 首行表头与当前程序列定义不一致；拒绝写入以免打乱或丢失历史。"""


# 首次写入快照时打一条日志，便于确认调度进程加载的是否为本仓库的 csv_logger（避免多副本/旧进程）
_snapshot_write_logged = False

# CSV 表头（固定顺序，必须与 build_snapshot_data 输出键一致）
CSV_HEADERS = [
    "时间",
    "实际交易动作",
    "60m交易",
    "是否持仓",
    "大盘状态",
    "代码",
    "名称",
    "现价",
    "日线风控",
    "客观缠论信号",
    "60m笔方向",
    "15分信号",
    "区间价格对齐",
    "决策理由",
    "日线A中枢ZD",
    "日线C中枢ZD",
    "锁定ZG",
    "15m_DIF",
    "15m_DEA",
    "底分型成立",
]

# 升级 CSV 前列定义（缺「60m交易」「区间价格对齐」）；命中时原地迁移而非整文件归档，避免主文件被「清空」观感
_SNAPSHOT_CSV_HEADERS_LEGACY_18 = (
    "时间",
    "实际交易动作",
    "是否持仓",
    "大盘状态",
    "代码",
    "名称",
    "现价",
    "日线风控",
    "客观缠论信号",
    "60m笔方向",
    "15分信号",
    "决策理由",
    "日线A中枢ZD",
    "日线C中枢ZD",
    "锁定ZG",
    "15m_DIF",
    "15m_DEA",
    "底分型成立",
)

# 状态映射：英文 → 中文
_MARKET_STATE_MAP = {
    "MARKET_SAFE": "安全",
    "MARKET_DANGER": "警戒",
    "MARKET_DEAD": "极度危险",
}

_TRADE_SIGNAL_MAP = {
    "BUY_1": "一买",
    "BUY_2": "二买",
    "BUY_3": "三买",
    "SELL": "卖出",
    "HOLD": "持仓",
    "IGNORE": "观望",
}

# 卖点细分映射（优先级：一卖 > 二卖 > 三卖）
_SELL_PRIORITY = ["first_sell", "second_sell", "third_sell"]
_SELL_SIGNAL_MAP = {
    "first_sell": "一卖",
    "second_sell": "二卖",
    "third_sell": "三卖",
}

# 买点细分映射（优先级：二买 > 三买 > 一买，与状态机一致）
_BUY_PRIORITY = ["second_buy", "third_buy", "first_buy"]
_BUY_SIGNAL_MAP = {
    "first_buy": "一买",
    "second_buy": "二买",
    "third_buy": "三买",
}


def _ensure_logs_dir() -> Path:
    """确保日志目录存在。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


@contextmanager
def _flock_snapshot_csv(path: Path) -> Iterator[None]:
    """防止 Gunicorn 调度与手动脚本同时追加同一快照 CSV 导致表头检测错乱。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_p = path.with_suffix(path.suffix + ".flock")
    with open(lock_p, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _check_is_holding(code: str) -> str:
    """
    检查标的是否在 watchlist.json 的 holdings 中。
    返回: "是" 或 "否"
    """
    try:
        watchlist_path = ROOT_DIR / "backend" / "data" / "watchlist.json"
        if watchlist_path.is_file():
            import json

            data = json.loads(watchlist_path.read_text(encoding="utf-8"))
            holdings = data.get("holdings", [])
            if any(str(item.get("code", "")).strip() == str(code).strip() for item in holdings):
                return "是"
    except Exception:
        pass
    return "否"


def _snapshot_watchlist_filename_suffix() -> str:
    """
    自选快照文件名后缀，来自环境变量 FIN_SNAPSHOT_CSV_SUFFIX。
    例：_new → snapshots_2026_new.csv；传入 new 时自动补前导下划线。
    仅允许字母数字下划线与单连字符，防路径注入。
    """
    raw = (os.environ.get("FIN_SNAPSHOT_CSV_SUFFIX") or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("_", "-")):
        raw = "_" + raw
    if not re.fullmatch(r"[_A-Za-z0-9-]+", raw):
        raise ValueError(f"FIN_SNAPSHOT_CSV_SUFFIX 非法: {raw!r}")
    return raw


def _get_csv_path(timestamp: Optional[datetime] = None) -> Path:
    """按年分文件：logs/snapshots_YYYY.csv；可选后缀见 _snapshot_watchlist_filename_suffix。"""
    year = (timestamp or datetime.now()).strftime("%Y")
    suf = _snapshot_watchlist_filename_suffix()
    return _ensure_logs_dir() / f"snapshots_{year}{suf}.csv"


def _get_hs300_csv_path(timestamp: Optional[datetime] = None) -> Path:
    """沪深300快照：logs/snapshots_hs300_YYYY.csv"""
    year = (timestamp or datetime.now()).strftime("%Y")
    return _ensure_logs_dir() / f"snapshots_hs300_{year}.csv"


def _normalize_snapshot_header_row(cells: list[str]) -> list[str]:
    """表头比较前规范化：去空白、去掉首格 ZWSP/BOM，避免与 CSV_HEADERS 误判一致。"""
    if not cells:
        return cells
    out = [c.strip().strip("\ufeff") for c in cells]
    return out


def _migrate_legacy_18_snapshot_file(path: Path, data_dict: Dict[str, Any]) -> None:
    """
    旧版 18 列表头 → 当前 CSV_HEADERS：整文件重写，历史行新列填空字符串，最后写入本次 data_dict。
    调用方须已持有 flock，且确认首行为 _SNAPSHOT_CSV_HEADERS_LEGACY_18。
    """
    legacy_list = list(_SNAPSHOT_CSV_HEADERS_LEGACY_18)
    migrated: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        hdr = _normalize_snapshot_header_row(next(reader, []))
        if hdr != legacy_list:
            raise ValueError("legacy 迁移时表头与预期不一致")
        for row in reader:
            if not row or not any(str(c).strip() for c in row):
                continue
            if len(row) < len(legacy_list):
                continue
            row = row[: len(legacy_list)]
            old = dict(zip(legacy_list, row))
            migrated.append({k: old.get(k, "") for k in CSV_HEADERS})
    migrated.append({k: data_dict.get(k, "") for k in CSV_HEADERS})
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for r in migrated:
            writer.writerow(r)
    logging.info(
        "csv_logger: 已从 18 列 legacy 快照升级为 %d 列，保留 %d 行历史并写入当前行，文件=%s",
        len(CSV_HEADERS),
        len(migrated) - 1,
        path.name,
    )


def _append_snapshot_csv_row(path: Path, data_dict: Dict[str, Any]) -> None:
    """
    将一行快照追加到指定 CSV；表头/备份逻辑与 log_snapshot 相同。
    供 log_snapshot / log_snapshot_hs300 共用。
    """
    with _flock_snapshot_csv(path):
        file_exists = path.is_file()

        if file_exists and path.stat().st_size > 0:
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.reader(f)
                    existing_headers = next(reader, [])
                normalized = _normalize_snapshot_header_row(existing_headers)

                if not normalized:
                    raise SnapshotCsvHeaderConflictError(
                        f"快照 CSV 首行无法解析为表头，已拒绝写入（不移动、不覆盖原文件）。路径: {path}"
                    )
                elif tuple(normalized) == _SNAPSHOT_CSV_HEADERS_LEGACY_18:
                    _migrate_legacy_18_snapshot_file(path, data_dict)
                    return
                elif normalized != CSV_HEADERS:
                    raise SnapshotCsvHeaderConflictError(
                        f"快照 CSV 表头与程序不一致（磁盘 {len(normalized)} 列 vs 程序 {len(CSV_HEADERS)} 列），"
                        f"已拒绝写入，避免覆盖或打乱历史。路径: {path}\n"
                        f"磁盘表头: {normalized}\n"
                        f"程序表头: {CSV_HEADERS}"
                    )
            except SnapshotCsvHeaderConflictError:
                raise
            except Exception:
                logging.warning("csv_logger: 表头检查失败", exc_info=True)

        # 新建文件用 utf-8-sig（Excel BOM）；已存在则追加用 utf-8，避免编码器再次写入 BOM 或混用符号
        append_enc = "utf-8" if file_exists else "utf-8-sig"
        with open(path, "a", newline="", encoding=append_enc) as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(data_dict)


def _to_chinese_market_state(state: str) -> str:
    return _MARKET_STATE_MAP.get(state, str(state))


def _to_chinese_trade_signal(
    state: str,
    sell_signals: Optional[Dict[str, bool]] = None,
    reason: str = "",
    chan_sig: str = "",
    pen_direction: str = "",
    h15_sig: str = "",
    price_alignment: str = "",
) -> str:
    """
    确定「实际交易动作」。

    判定顺序：
    1) 买入：60m 信号不含「卖」+ 笔向下 + 15 分底背驰 + 区间对齐是 → 买入
    2) 卖出（完美止盈）：60m 信号不含「买」+ 笔向上 + 15 分顶背驰 + 区间对齐是 → 卖出
    3) 否则 观望
    """
    has_sell = "卖" in chan_sig
    has_buy = "买" in chan_sig

    if (
        not has_sell
        and pen_direction == "向下"
        and h15_sig == "底背驰"
        and price_alignment == "是"
    ):
        return "买入"

    if (
        not has_buy
        and pen_direction == "向上"
        and h15_sig == "顶背驰"
        and price_alignment == "是"
    ):
        return "卖出"

    return "观望"


def _fmt_float(value: Any) -> str:
    """将值格式化为两位小数字符串；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""


def _fmt_float4(value: Any) -> str:
    """将值格式化为四位小数字符串（用于 DIF/DEA）；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""

# 客观缠论信号拆分用：仅当全部为买点标签时才允许 60m「买入」
_BUY_LABELS_60M = frozenset({"一买", "二买", "三买"})


def _chan_signal_is_only_buy_types(chan_sig: str) -> bool:
    """
    True：信号串中仅含一买/二买/三买（可多项用 + 连接），不含一卖/二卖/三卖等。
    """
    if not chan_sig or chan_sig == "无信号":
        return False
    parts = [p.strip() for p in chan_sig.split("+") if p.strip()]
    if not parts:
        return False
    return all(p in _BUY_LABELS_60M for p in parts)


def _get_60m_trade_action(chan_sig: str, pen_direction: str) -> str:
    """
    计算60m交易动作（简化版）：
    - 不考虑15分信号和区间价格对齐
    - 卖出：60m 笔向上且客观缠论信号含卖点（与此前一致）
    - 买入：仅当客观缠论信号**仅为**一买/二买/三买的组合（无卖类）且 60m 笔向下
    """
    has_sell = "卖" in chan_sig
    # 笔向上：看卖信号（不变）
    if pen_direction == "向上" and has_sell:
        return "卖出"
    # 笔向下：仅纯买点类型时视为 60m 买入
    if pen_direction == "向下" and _chan_signal_is_only_buy_types(chan_sig):
        return "买入"
    return "观望"



def _h15_signal_detail(
    h15_result: Optional[Dict[str, Any]],
    *,
    return_trace: bool = False,
) -> Dict[str, Any]:
    """
    独立计算15分钟背驰信号，仅依赖15分钟K线、笔和MACD数据。
    返回包含信号和当前笔极端价格的字典。

    规则：
    - 底背驰：当前向下笔 + 价格创新低 + 动能衰竭(面积背驰或黄白线背离) + 底分型确认
    - 顶背驰：当前向上笔 + 价格创新高 + 动能衰竭(面积背驰或黄白线背离) + 顶分型确认
    - 不满足时返回 "无信号"

    返回值：{"signal": str, "extreme_price": Optional[float]}；若 return_trace=True 另含 "trace": [str, ...] 逐步说明。
    """
    trace: list[str] = []

    def tr(msg: str) -> None:
        if return_trace:
            trace.append(msg)

    def finish(res: Dict[str, Any]) -> Dict[str, Any]:
        if return_trace:
            out = dict(res)
            out["trace"] = trace
            return out
        return res

    result: Dict[str, Any] = {"signal": "无信号", "extreme_price": None}

    if not h15_result:
        tr("h15_result 为空")
        return finish(result)

    data = h15_result.get("data", [])
    pens = h15_result.get("pens", [])
    pens_effective = h15_result.get("pens_effective", [])
    fractals = h15_result.get("fractals", [])

    tr(f"15m K 根数={len(data)} pens_effective={len(pens_effective)} fractals={len(fractals)}")

    if not data or len(data) < 3 or not pens_effective:
        tr("数据不足：K<3 或无 pens_effective")
        return finish(result)

    # 构建日期到索引的映射
    date_to_idx = {item["date"]: i for i, item in enumerate(data)}

    # 获取有效笔（至少完成两根K线）
    effective_pens = [p for p in pens_effective if p.get("direction") in ("up", "down")]
    if len(effective_pens) < 2:
        tr(f"有效方向笔不足2：effective_pens={len(effective_pens)}")
        return finish(result)

    # 当前笔：最新的一笔
    current_pen = effective_pens[-1]
    current_direction = current_pen.get("direction")

    # 同向对比笔：与当前笔方向相同的前一笔
    same_dir_pens = [p for p in effective_pens if p.get("direction") == current_direction]
    if len(same_dir_pens) < 2:
        tr(f"同向笔不足2：当前方向={current_direction!r} 同向笔数={len(same_dir_pens)}")
        return finish(result)
    compare_pen = same_dir_pens[-2]

    tr(
        "当前笔 "
        f"dir={current_direction!r} {current_pen.get('start_date')}→{current_pen.get('end_date')} "
        f"价 {current_pen.get('start_price')}→{current_pen.get('end_price')} | "
        "对比笔(同向前一笔) "
        f"{compare_pen.get('start_date')}→{compare_pen.get('end_date')} "
        f"价 {compare_pen.get('start_price')}→{compare_pen.get('end_price')}"
    )

    # 辅助函数：计算笔的MACD面积
    def calc_macd_area(pen: Dict[str, Any], is_green: bool) -> float:
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        area = 0.0
        for item in data[s_idx:e_idx + 1]:
            m = item.get("macd", {}).get("macd")
            if m is not None:
                if is_green and m < 0:
                    area += abs(m)
                elif not is_green and m > 0:
                    area += abs(m)
        return area

    # 辅助函数：计算笔内DIF极值
    def get_dif_extreme(pen: Dict[str, Any], find_max: bool) -> float:
        s_idx = date_to_idx.get(pen.get("start_date"))
        e_idx = date_to_idx.get(pen.get("end_date"))
        if s_idx is None or e_idx is None or s_idx > e_idx:
            return 0.0
        dif_values = [
            item.get("macd", {}).get("dif")
            for item in data[s_idx:e_idx + 1]
            if item.get("macd", {}).get("dif") is not None
        ]
        if not dif_values:
            return 0.0
        return max(dif_values) if find_max else min(dif_values)

    # 辅助函数：检查笔末端是否有分型
    def has_fractal_at_end(pen: Dict[str, Any], fractal_type: str) -> bool:
        pen_end_date = pen.get("end_date")
        if not pen_end_date or not fractals:
            return False
        for f in fractals:
            if f.get("type") == fractal_type and f.get("date") == pen_end_date:
                return True
        return False

    # ========== 逻辑 1：底背驰判断 ==========
    if current_direction == "down":
        tr("底背驰分支：当前为向下笔")
        # 必须先计算MACD面积，确保当前笔有实际的下跌动能
        current_area = calc_macd_area(current_pen, is_green=True)
        compare_area = calc_macd_area(compare_pen, is_green=True)
        tr(f"绿柱面积 当前笔={current_area:.6f} 对比笔={compare_area:.6f}")

        # 严格条件：当前笔必须有绿柱（有实际的下跌动能）
        if current_area <= 0:
            tr("否决：当前笔绿柱面积<=0（无有效下跌动能）")
            pass  # 继续后续逻辑，最终会返回"无信号"
        else:
            # 检查是否动能加速：取最近3个同向笔，如果当前笔面积最大，则不是背驰
            recent_3_areas = [calc_macd_area(p, is_green=True) for p in same_dir_pens[-3:]]  # 最近3个（含当前笔）
            max_recent_area = max(recent_3_areas) if recent_3_areas else 0
            tr(f"近{len(same_dir_pens[-3:])}根同向下笔绿柱面积={['%.4f' % a for a in recent_3_areas]} max={max_recent_area:.6f}")
            if current_area >= max_recent_area:
                tr("否决：当前笔绿柱面积为近3根同向笔中最大或并列最大（动能未衰竭）")
                pass  # 继续后续逻辑，最终会返回"无信号"
            else:
                # 条件1：方向与空间 - 当前笔最低价 < 对比笔最低价（创新低）
                current_low = min(
                    float(current_pen.get("start_price", 0)),
                    float(current_pen.get("end_price", 0))
                )
                compare_low = min(
                    float(compare_pen.get("start_price", 0)),
                    float(compare_pen.get("end_price", 0))
                )
                tr(f"笔低价 当前={current_low:.4f} 对比={compare_low:.4f}")
                if current_low < compare_low:
                    # 条件2：动能衰竭（满足其一即可）
                    # 面积背驰：当前绿柱面积 < 对比笔绿柱面积（必须两笔都有绿柱）
                    area_divergence = compare_area > 0 and current_area < compare_area

                    # 黄白线背离：当前DIF最低值 > 对比笔DIF最低值（要求对比笔也有下跌动能）
                    current_dif_min = get_dif_extreme(current_pen, find_max=False)
                    compare_dif_min = get_dif_extreme(compare_pen, find_max=False)
                    # 只有当对比笔也有绿柱时，DIF比较才有意义
                    dif_divergence = compare_area > 0 and current_dif_min > compare_dif_min

                    tr(
                        "背驰子条件 "
                        f"面积背驰={area_divergence} DIF背离={dif_divergence} "
                        f"(DIF_min 当前={current_dif_min:.6f} 对比={compare_dif_min:.6f})"
                    )

                    # 条件3：右侧确认 - 底分型
                    has_bottom = has_fractal_at_end(current_pen, "bottom")
                    tr(f"笔末端底分型={has_bottom} (end_date={current_pen.get('end_date')})")

                    if has_bottom and (area_divergence or dif_divergence):
                        result["signal"] = "底背驰"
                        result["extreme_price"] = current_low
                        tr("结论：底背驰成立")
                        return finish(result)
                    tr("否决：无底分型或面积/DIF背驰均未满足")
                else:
                    tr("否决：当前笔低价未低于对比笔（未创新低）")

    # ========== 逻辑 2：顶背驰判断 ==========
    if current_direction == "up":
        tr("顶背驰分支：当前为向上笔")
        # 必须先计算MACD面积，确保当前笔有实际的上涨动能
        current_area = calc_macd_area(current_pen, is_green=False)
        compare_area = calc_macd_area(compare_pen, is_green=False)
        tr(f"红柱面积 当前笔={current_area:.6f} 对比笔={compare_area:.6f}")

        # 严格条件：当前笔必须有红柱（有实际的上涨动能）
        if current_area <= 0:
            tr("否决：当前笔红柱面积<=0（无有效上涨动能）")
            pass  # 继续后续逻辑，最终会返回"无信号"
        else:
            # 检查是否动能加速：取最近3个同向笔，如果当前笔面积最大，则不是背驰
            recent_3_areas = [calc_macd_area(p, is_green=False) for p in same_dir_pens[-3:]]  # 最近3个（含当前笔）
            max_recent_area = max(recent_3_areas) if recent_3_areas else 0
            tr(f"近{len(same_dir_pens[-3:])}根同向上笔红柱面积={['%.4f' % a for a in recent_3_areas]} max={max_recent_area:.6f}")
            if current_area >= max_recent_area:
                tr("否决：当前笔红柱面积为近3根同向笔中最大或并列最大（动能未衰竭）")
                pass  # 继续后续逻辑，最终会返回"无信号"
            else:
                # 条件1：方向与空间 - 当前笔最高价 > 对比笔最高价（创新高）
                current_high = max(
                    float(current_pen.get("start_price", 0)),
                    float(current_pen.get("end_price", 0))
                )
                compare_high = max(
                    float(compare_pen.get("start_price", 0)),
                    float(compare_pen.get("end_price", 0))
                )
                tr(f"笔高价 当前={current_high:.4f} 对比={compare_high:.4f}")
                if current_high > compare_high:
                    # 条件2：动能衰竭（满足其一即可）
                    # 面积背驰：当前红柱面积 < 对比笔红柱面积（必须两笔都有红柱）
                    area_divergence = compare_area > 0 and current_area < compare_area

                    # 黄白线背离：当前DIF最高值 < 对比笔DIF最高值（要求对比笔也有上涨动能）
                    current_dif_max = get_dif_extreme(current_pen, find_max=True)
                    compare_dif_max = get_dif_extreme(compare_pen, find_max=True)
                    # 只有当对比笔也有红柱时，DIF比较才有意义
                    dif_divergence = compare_area > 0 and current_dif_max < compare_dif_max

                    tr(
                        "背驰子条件 "
                        f"面积背驰={area_divergence} DIF背离={dif_divergence} "
                        f"(DIF_max 当前={current_dif_max:.6f} 对比={compare_dif_max:.6f})"
                    )

                    # 条件3：右侧确认 - 顶分型
                    has_top = has_fractal_at_end(current_pen, "top")
                    tr(f"笔末端顶分型={has_top} (end_date={current_pen.get('end_date')})")

                    if has_top and (area_divergence or dif_divergence):
                        result["signal"] = "顶背驰"
                        result["extreme_price"] = current_high
                        tr("结论：顶背驰成立")
                        return finish(result)
                    tr("否决：无顶分型或面积/DIF背驰均未满足")
                else:
                    tr("否决：当前笔高价未高于对比笔（未创新高）")

    # ========== 逻辑 3：无信号 ==========
    tr("结论：无信号（未满足底/顶背驰全部条件）")
    return finish(result)


def _h15_signal(h15_result: Optional[Dict[str, Any]]) -> str:
    """
    独立计算15分钟背驰信号，返回信号字符串（向后兼容）。
    """
    return _h15_signal_detail(h15_result).get("signal", "无信号")


# 区间价格对齐：相对容差（|Δ|/|基准价|），默认千分之五；基准价过小时回退绝对差，避免除零或毛刺
_PRICE_ALIGN_REL_FRACTION = 0.005
_PRICE_ALIGN_ABS_FALLBACK = 0.01
_REF_ABS_FLOOR = 1e-6


def _price_interval_aligned(abs_diff: float, ref_level: float) -> bool:
    """
    True：|abs_diff| / |ref_level| <= 千分之五（0.005）；若 |ref_level| 过小则改用 |abs_diff| <= 0.01。
    """
    try:
        r = abs(float(ref_level))
        d = abs(float(abs_diff))
        if r < _REF_ABS_FLOOR:
            return d <= _PRICE_ALIGN_ABS_FALLBACK
        return d / r <= _PRICE_ALIGN_REL_FRACTION
    except (TypeError, ValueError):
        return False


def _price_alignment(
    h15_sig: str,
    pen_dir: str,
    h15_result: Optional[Dict[str, Any]],
    h60_result: Optional[Dict[str, Any]],
) -> str:
    """
    计算「区间价格对齐」字段值。

    逻辑：
    - 如果 15分信号 == '无信号'，输出 '-'。
    - 如果 15分信号 == '底背驰' 且 60m笔方向 == '向下'：
        提取 15m 触发底背驰的向下笔的最低价（15m_low）。
        提取 60m 当前向下笔的最低价（60m_low）。
        若 |15m_low - 60m_low| / |60m_low| <= 0.005 为「是」（|60m_low| 极小时回退 |差|<=0.01）。
    - 如果 15分信号 == '顶背驰' 且 60m笔方向 == '向上'：
        提取 15m 触发顶背驰的向上笔的最高价（15m_high）。
        提取 60m 当前向上笔的最高价（60m_high）。
        同上相对千分之五规则（高价为基准）。
    - 其他任何方向不匹配的情况，一律输出 '否'。
    """
    # 无信号时返回 '-'
    if h15_sig == "无信号":
        return "-"

    # 获取15分钟信号详情（包含极端价格）
    h15_detail = _h15_signal_detail(h15_result)
    h15_signal_type = h15_detail.get("signal", "无信号")
    h15_extreme_price = h15_detail.get("extreme_price")

    # 底背驰情况
    if h15_sig == "底背驰":
        if pen_dir != "向下":
            return "否"
        if h15_extreme_price is None:
            return "否"
        # 获取60分钟向下笔的最低价
        m60_low = _get_60m_pen_extreme_price(h60_result, "向下")
        if m60_low is None:
            return "否"
        if _price_interval_aligned(h15_extreme_price - m60_low, m60_low):
            return "是"
        return "否"

    # 顶背驰情况
    if h15_sig == "顶背驰":
        if pen_dir != "向上":
            return "否"
        if h15_extreme_price is None:
            return "否"
        # 获取60分钟向上笔的最高价
        m60_high = _get_60m_pen_extreme_price(h60_result, "向上")
        if m60_high is None:
            return "否"
        if _price_interval_aligned(h15_extreme_price - m60_high, m60_high):
            return "是"
        return "否"

    # 其他情况
    return "否"


def _defense_detail(analysis: Dict[str, Any]) -> str:
    """
    防线偏离详情：计算现价与 min(A-ZD, C-ZD) 的偏离幅度。
    与状态机保持一致，优先使用 latest_close。
    """
    try:
        check_price = float(analysis.get("latest_close") or analysis.get("daily_close"))
        daily_azd = float(analysis.get("daily_azd"))
        daily_czd = float(analysis.get("daily_czd"))
        min_zd = min(daily_azd, daily_czd)
        if min_zd != 0:
            deviation = (check_price - min_zd) / min_zd * 100
            return f"min-ZD({min_zd:.2f})，现价{check_price:.2f}偏离{deviation:+.2f}%"
    except (TypeError, ValueError):
        pass
    return ""


def _core_reason(analysis: Dict[str, Any], market_state: str = "") -> str:
    """
    从状态机分析结果中提取风控驱动的核心原因（增强版）。
    匹配优先级（高→低）：跌破min-ZD > 顶背驰 > 买点确认
    增强内容：防线偏离幅度、级别对齐详情、笔方向。
    """
    state = analysis.get("state", "")
    reason = analysis.get("reason") or ""

    # 辅助：级别对齐详情
    def _align_detail() -> str:
        h15_align = analysis.get("h15_level_alignment")
        if h15_align:
            align_reason = getattr(h15_align, "reason", "")
            if align_reason:
                return align_reason
        return ""

    # 辅助：笔方向
    def _pen_dir() -> str:
        h60_conditions = analysis.get("h60_conditions") or {}
        return "向上笔" if h60_conditions.get("last_pen_up") else "向下笔"

    if "跌破战略底线" in reason or "跌破 min-ZD" in reason:
        detail = _defense_detail(analysis)
        base = "跌破战略底线，强制清仓" if state == "SELL" else "跌破战略底线，拉黑"
        return f"{base} | {detail}" if detail else base

    if "顶背驰" in reason:
        align = _align_detail()
        base = "60分钟向上笔+15分钟顶背驰"
        return f"{base} | {align}" if align else base

    if "一买确认" in reason or "二买确认" in reason or "三买确认" in reason:
        return reason

    if "无买卖点" in reason or "中枢震荡" in reason:
        pen_dir = _pen_dir()
        return f"{reason}，{pen_dir}" if pen_dir else reason

    if "持仓中" in reason:
        pen_dir = _pen_dir()
        return f"{reason}，{pen_dir}" if pen_dir else reason

    # 最终兜底
    return reason


def _chan_signal(
    buy_signals: Optional[Dict[str, bool]] = None,
    sell_signals: Optional[Dict[str, bool]] = None,
) -> str:
    """
    根据独立的买卖点检测结果生成纯缠论信号。
    不受状态机/风控影响，仅反映当前60分钟K线结构上的缠论买卖点。
    同时检测到多个信号时，用'+'连接显示所有信号。
    """
    signals: list[str] = []
    
    # 卖点信号（按优先级顺序）
    if sell_signals is not None:
        for key in _SELL_PRIORITY:
            if sell_signals.get(key):
                signals.append(_SELL_SIGNAL_MAP.get(key, ""))
    
    # 买点信号（按优先级顺序）
    if buy_signals is not None:
        for key in _BUY_PRIORITY:
            if buy_signals.get(key):
                signals.append(_BUY_SIGNAL_MAP.get(key, ""))
    
    return "+".join(signals) if signals else "无信号"


def _daily_risk_level(analysis: Dict[str, Any], price: Any = None) -> str:
    """
    根据现价与 MIN(A-ZD, C-ZD) 的关系映射日线风控状态。
    15分钟调度后，用最新价格（优先15分钟收盘价）与最低防线比较。
    """
    daily_czd = analysis.get("daily_czd")
    daily_azd = analysis.get("daily_azd")
    if daily_czd is None or daily_azd is None:
        return "安全"
    try:
        current_price = float(price) if price is not None else float(analysis.get("daily_close") or 0)
        czd = float(daily_czd)
        azd = float(daily_azd)
        min_zd = min(azd, czd)
        if current_price < min_zd:
            return "日线破位"
        return "安全"
    except (TypeError, ValueError):
        return "安全"


def _pen_direction(analysis: Dict[str, Any]) -> str:
    """60分钟最后一笔有效笔方向。"""
    h60_conditions = analysis.get("h60_conditions") or {}
    return "向上" if h60_conditions.get("last_pen_up") else "向下"


def _get_60m_pen_extreme_price(h60_result: Optional[Dict[str, Any]], direction: str) -> Optional[float]:
    """
    获取60分钟当前笔的极端价格。
    - 方向为"向下"时，返回笔的最低价（start_price和end_price的较小值）
    - 方向为"向上"时，返回笔的最高价（start_price和end_price的较大值）
    """
    if not h60_result:
        return None
    pens_effective = h60_result.get("pens_effective", [])
    if not pens_effective:
        return None

    # 获取最后一笔
    current_pen = pens_effective[-1]
    pen_direction = current_pen.get("direction")

    # 检查笔方向是否与预期一致
    expected_direction = "up" if direction == "向上" else "down"
    if pen_direction != expected_direction:
        return None

    start_price = float(current_pen.get("start_price", 0))
    end_price = float(current_pen.get("end_price", 0))

    if direction == "向下":
        return min(start_price, end_price)
    else:  # 向上
        return max(start_price, end_price)


def _locked_zg(h60_result: Optional[Dict[str, Any]]) -> str:
    """60分钟最新中枢的 ZG（锁定ZG）。"""
    if not h60_result or not h60_result.get("centrals"):
        return ""
    try:
        sorted_c = sorted(
            h60_result["centrals"],
            key=lambda c: c.get("form_end_date") or c.get("end_date", ""),
        )
        return _fmt_float(sorted_c[-1].get("zg"))
    except Exception:
        return ""


def _h15_macd(h15_result: Optional[Dict[str, Any]]) -> tuple[str, str]:
    """15分钟最新K线的 DIF 与 DEA。"""
    if not h15_result or not h15_result.get("data"):
        return "", ""
    try:
        macd = h15_result["data"][-1].get("macd", {})
        dif = macd.get("dif")
        dea = macd.get("dea")
        return _fmt_float4(dif), _fmt_float4(dea)
    except Exception:
        return "", ""


def _has_bottom_fractal(h15_result: Optional[Dict[str, Any]]) -> str:
    """15分钟最近一根K线是否有底分型确认。"""
    if not h15_result or not h15_result.get("data") or not h15_result.get("fractals"):
        return "否"
    try:
        last_date = h15_result["data"][-1].get("date")
        if not last_date:
            return "否"
        for f in h15_result["fractals"]:
            if f.get("type") == "bottom" and f.get("date") == last_date:
                return "是"
        return "否"
    except Exception:
        return "否"


def _build_smart_reason(
    market_state: str,
    analysis: Dict[str, Any],
    chan_sig: str,
    h15_sig: str,
    trade_sig: str,
    pen_direction: str,
    price_alignment: str,
) -> str:
    """
    决策理由生成：买入 / 卖出（完美止盈）/ 观望。
    """
    has_buy = "买" in chan_sig
    has_sell = "卖" in chan_sig

    if trade_sig == "买入":
        return "宏观战略锁定，微观动能衰竭，时空完美共振！"

    if trade_sig == "卖出":
        return "宏观遇阻，微观多头力竭，时空完美共振！"

    reasons: list[str] = []

    if not has_buy and not has_sell:
        reasons.append("客观缠论无买/卖关键字或「无信号」")
    if has_sell:
        reasons.append("客观缠论信号含「卖」")
    elif pen_direction != "向下":
        reasons.append(f"60m笔方向为{pen_direction}（买入须向下）")
    if has_sell and pen_direction != "向上":
        reasons.append(f"卖点但笔方向{pen_direction}")

    if h15_sig == "无信号":
        reasons.append("15分无背驰")
    elif not has_sell and pen_direction == "向下" and h15_sig != "底背驰":
        reasons.append(f"15分{h15_sig}（买入需底背驰）")
    elif not has_buy and pen_direction == "向上" and h15_sig != "顶背驰":
        reasons.append(f"15分{h15_sig}（完美止盈需顶背驰）")

    if price_alignment != "是":
        reasons.append(f"区间价格对齐{price_alignment}")

    if (
        has_buy
        and pen_direction == "向上"
        and h15_sig == "顶背驰"
        and price_alignment == "是"
    ):
        reasons.append("完美止盈（模块A）未满足：客观缠论含「买」")

    if reasons:
        return f"4条件未全部满足（{'；'.join(reasons)}），继续观望"

    return "条件未全部满足，继续观望"


def build_snapshot_data(
    timestamp: datetime,
    code: str,
    name: str,
    market_state: str,
    analysis: Dict[str, Any],
    h60_result: Optional[Dict[str, Any]],
    h15_result: Optional[Dict[str, Any]],
    sell_signals: Optional[Dict[str, bool]] = None,
    buy_signals: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """
    将状态机分析结果拍平为 CSV 行字典。
    返回的字典中全为标量（字符串/数字），无内存引用污染。
    """
    analysis = copy.deepcopy(analysis)
    h60_result = copy.deepcopy(h60_result) if h60_result else None
    h15_result = copy.deepcopy(h15_result) if h15_result else None

    # 现价：直接使用状态机已计算好的 latest_close（优先级 15m > 60m > 日线），避免与决策逻辑分歧
    price = analysis.get("latest_close") or analysis.get("daily_close")

    dif, dea = _h15_macd(h15_result)

    chan_sig = _chan_signal(buy_signals, sell_signals)
    h15_sig = _h15_signal(h15_result)
    pen_dir = _pen_direction(analysis)

    # 先计算区间价格对齐（4条件判断需要）
    price_alignment = _price_alignment(h15_sig, pen_dir, h15_result, h60_result)

    # 基于4条件判断确定交易信号
    trade_sig = _to_chinese_trade_signal(
        analysis.get("state", "IGNORE"),
        sell_signals,
        analysis.get("reason", ""),
        chan_sig,
        pen_dir,
        h15_sig,
        price_alignment,
    )

    # 基于4条件判断生成决策理由
    smart_reason = _build_smart_reason(
        market_state, analysis, chan_sig, h15_sig, trade_sig, pen_dir, price_alignment
    )

    # 判断是否持仓（从 watchlist.json 的 holdings 中检查）
    is_holding = _check_is_holding(code)

    return {
        "时间": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "实际交易动作": trade_sig,
        "60m交易": _get_60m_trade_action(chan_sig, pen_dir),
        "是否持仓": is_holding,
        "大盘状态": _to_chinese_market_state(market_state),
        "代码": str(code),
        "名称": str(name),
        "现价": _fmt_float(price),
        "日线风控": _daily_risk_level(analysis, price),
        "客观缠论信号": chan_sig,
        "60m笔方向": _pen_direction(analysis),
        "15分信号": h15_sig,
        "区间价格对齐": price_alignment,
        "决策理由": smart_reason,
        "日线A中枢ZD": _fmt_float(analysis.get("daily_azd")),
        "日线C中枢ZD": _fmt_float(analysis.get("daily_czd")),
        "锁定ZG": _locked_zg(h60_result),
        "15m_DIF": dif,
        "15m_DEA": dea,
        "底分型成立": _has_bottom_fractal(h15_result),
    }


def _read_last_csv_time(path: Path) -> Optional[str]:
    """读取 CSV 最后一行的第一个字段（时间戳），通过 seek 到文件末尾避免全文件扫描。"""
    try:
        with open(path, "rb") as f:
            # 定位到文件末尾前 4KB（足够覆盖最后一行）
            f.seek(0, 2)
            file_size = f.tell()
            seek_pos = max(0, file_size - 4096)
            f.seek(seek_pos)
            # 如果是从文件中间开始读，先丢弃第一行（可能不完整）
            if seek_pos > 0:
                f.readline()
            lines = f.read().decode("utf-8-sig").splitlines()
            # 从后往前找第一个非空行
            for line in reversed(lines):
                line = line.strip()
                if line:
                    # 取第一个逗号前的内容即时间戳
                    return line.split(",")[0] if "," in line else line
    except Exception:
        logging.debug("csv_logger: 读取最后一行时间戳失败", exc_info=True)
    return None


def append_watchlist_snapshot_batch_separator_line() -> None:
    """
    自选快照 CSV 已有内容时，在本批首行数据写入前先追加一行空行，便于区分多次 run。
    与 log_snapshot 共用 flock，避免与并发追加交错。
    """
    path = _get_csv_path()
    try:
        with _flock_snapshot_csv(path):
            if not path.is_file() or path.stat().st_size == 0:
                return
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write("\n")
    except Exception:
        logging.warning("csv_logger: 自选快照批次前空行追加失败", exc_info=True)


def log_snapshot(data_dict: Dict[str, Any]) -> None:
    """
    将快照字典追加写入 CSV。文件不存在时自动写入表头。
    表头与程序冲突时抛出 SnapshotCsvHeaderConflictError（不移动、不覆盖已有文件）。
    其它 I/O 异常仍记录日志，尽量不阻塞主流程。
    """
    global _snapshot_write_logged
    try:
        if not _snapshot_write_logged:
            _snapshot_write_logged = True
            logging.info(
                "csv_logger: 自选快照列定义 %d 列（60m交易/区间价格对齐），模块路径 %s",
                len(CSV_HEADERS),
                Path(__file__).resolve(),
            )
        time_str = data_dict.get("时间", "")
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S") if time_str else datetime.now()
        path = _get_csv_path(dt)
        _append_snapshot_csv_row(path, data_dict)
    except SnapshotCsvHeaderConflictError:
        raise
    except Exception:
        logging.warning("csv_logger: 快照写入失败", exc_info=True)


def log_snapshot_hs300(data_dict: Dict[str, Any]) -> None:
    """与同批 snapshots_YYYY.csv 表头完全一致；写入 logs/snapshots_hs300_YYYY.csv。"""
    global _snapshot_write_logged
    try:
        if not _snapshot_write_logged:
            _snapshot_write_logged = True
            logging.info(
                "csv_logger: HS300 快照列定义 %d 列（与自选一致），模块路径 %s",
                len(CSV_HEADERS),
                Path(__file__).resolve(),
            )
        time_str = data_dict.get("时间", "")
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S") if time_str else datetime.now()
        path = _get_hs300_csv_path(dt)
        _append_snapshot_csv_row(path, data_dict)
    except SnapshotCsvHeaderConflictError:
        raise
    except Exception:
        logging.warning("csv_logger: HS300 快照写入失败", exc_info=True)
