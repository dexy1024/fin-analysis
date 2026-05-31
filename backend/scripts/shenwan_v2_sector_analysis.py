#!/usr/bin/env python3
"""
申万二级行业指数：抓取行业列表 → 拉取日 K → 技术面量化打标 → 导出 CSV。

步骤一：新浪财经 Market_Center.getHQNodes 抓取「申万二级」板块指数，
       再用 Market_Center.getHQNodeData 分页拉取各行业成分 A 股，
       保存为 shenwan_v2_sectors.json（含 sector_code / sector_name / stocks）。

步骤二：读取 JSON，用新浪 getKLineData 拉取 sh000300 及各行业日 K，
       计算相对强度与均线多头打标，输出 01_shenwan_v2_analysis_result.csv（当日快照，仅是否综合满足=1），
       并追加 logs/02_shenwan_v2_analysis_history.csv（按数据日期 upsert 的长表，仅是否综合满足=1）。

说明：新浪 getKLineData 对 sw2_* 板块指数通常返回 null；脚本会先尝试新浪
      （含 801 代码变体），再回退申万宏源官方指数 trend 接口（与 akshare 同源）。

用法：
    python3 backend/scripts/shenwan_v2_sector_analysis.py
    python3 backend/scripts/shenwan_v2_sector_analysis.py -o . --force-refresh-sectors
    python3 backend/scripts/shenwan_v2_sector_analysis.py --sectors-only --refresh-stocks
    python3 backend/scripts/shenwan_v2_sector_analysis.py --analysis-only -o .
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

import akshare as ak
import pandas as pd
import requests

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

SINA_HQ_NODES_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodes"
)
SINA_HQ_NODE_DATA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
STOCK_PAGE_SIZE = 100
STOCK_MAX_PAGES = 50
STOCK_FETCH_SLEEP_SEC = 0.5
STOCK_RATE_LIMIT_STATUSES = (403, 429, 456)
STOCK_RATE_LIMIT_BACKOFF_SEC = 8.0
STABLE_STOCK_FETCH_SLEEP_SEC = 0.85
STABLE_RATE_LIMIT_BACKOFF_SEC = 12.0
SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
SINA_KLINE_URL_ALT = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
)
SWHY_TREND_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"

BENCHMARK_SYMBOL = "sh000300"
TRADING_DAYS = 100
RETURN_WINDOW = 20
KLINE_FETCH_LEN = 120

SECTORS_JSON = "shenwan_v2_sectors.json"
SECTOR_CODES_JSON = "shenwan_v2_sector_codes.json"
RESULT_CSV = "01_shenwan_v2_analysis_result.csv"
RESULT_HISTORY_CSV = "logs/02_shenwan_v2_analysis_history.csv"
HISTORY_DATE_COL = "数据日期"
HISTORY_KEY_COLS = (HISTORY_DATE_COL, "行业代码")

ANALYSIS_RESULT_COLUMNS = (
    HISTORY_DATE_COL,
    "行业代码",
    "行业名称",
    "近20日涨幅",
    "超额收益率",
    "是否跑赢大盘",
    "是否均线多头",
    "是否综合满足",
)


def _reorder_date_column_first(df: pd.DataFrame, date_col: str = HISTORY_DATE_COL) -> pd.DataFrame:
    """将数据日期列置于表头最前，其余列保持原顺序。"""
    if date_col not in df.columns:
        return df
    rest = [c for c in df.columns if c != date_col]
    return df[[date_col, *rest]]


def warn_if_sector_data_lags(
    benchmark: pd.DataFrame,
    result_df: pd.DataFrame,
    *,
    label: str = "量化打标",
) -> None:
    """行业指数源常晚于沪深300更新；数据日期落后时长表只会 upsert 同日，不会追加新日。"""
    if result_df.empty or HISTORY_DATE_COL not in result_df.columns:
        return
    bench_last = pd.to_datetime(benchmark["date"].iloc[-1]).strftime("%Y-%m-%d")
    sector_last = str(result_df[HISTORY_DATE_COL].max())
    if sector_last >= bench_last:
        return
    logging.warning(
        "%s：行业指数 K 线最新仅至 %s（沪深300 已至 %s）。"
        "长表按「数据日期 + 行业代码」upsert——同日重跑覆盖旧行，不会追加 %s。"
        "请待申万指数源更新后重跑。",
        label,
        sector_last,
        bench_last,
        bench_last,
    )


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}


def write_snapshot_and_append_history(
    history_df: pd.DataFrame,
    history_path: Path,
    *,
    snapshot_df: pd.DataFrame | None = None,
    snapshot_path: Path | None = None,
    sort_snapshot_by: str | None = None,
) -> None:
    """
    按「数据日期 + 行业代码」 upsert 到历史长表；可选覆盖写入当日快照 CSV。
    同一数据日期重复运行会替换该日旧行，避免重复追加。
    """
    if HISTORY_DATE_COL not in history_df.columns:
        raise ValueError(f"缺少列 {HISTORY_DATE_COL!r}，无法写入历史长表")

    hist = _reorder_date_column_first(history_df.reset_index(drop=True))

    if snapshot_path is not None:
        snap_src = hist if snapshot_df is None else snapshot_df
        if sort_snapshot_by:
            snap = snap_src.sort_values(sort_snapshot_by, ascending=False).reset_index(drop=True)
        else:
            snap = snap_src.reset_index(drop=True)
        snap = _reorder_date_column_first(snap)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snap.to_csv(snapshot_path, index=False, encoding="utf-8-sig")
    dates = hist[HISTORY_DATE_COL].astype(str).unique().tolist()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    if history_path.is_file():
        try:
            old = pd.read_csv(history_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            old = pd.read_csv(history_path, encoding="utf-8")
        if HISTORY_DATE_COL in old.columns:
            old = old[~old[HISTORY_DATE_COL].astype(str).isin(dates)]
        else:
            logging.warning("历史 CSV 缺列 %s，将丢弃旧内容", HISTORY_DATE_COL)
            old = pd.DataFrame()
    else:
        old = pd.DataFrame()

    combined = pd.concat([old, hist], ignore_index=True)
    combined[HISTORY_DATE_COL] = combined[HISTORY_DATE_COL].astype(str)
    sort_cols = [HISTORY_DATE_COL]
    if "行业代码" in combined.columns:
        sort_cols.append("行业代码")
    combined = combined.sort_values(sort_cols, ascending=[False] + [True] * (len(sort_cols) - 1))
    combined = _reorder_date_column_first(combined.reset_index(drop=True))
    combined.to_csv(history_path, index=False, encoding="utf-8-sig")

    logging.info(
        "历史长表 upsert %d 行（数据日期 %s）→ %s，合计 %d 行",
        len(hist),
        ", ".join(dates),
        history_path,
        len(combined),
    )


def _with_retry(fetch_fn, *, retries: int = 3, sleep_sec: float = 0.5):
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fetch_fn()
        except EXPECTED_BUSINESS_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(sleep_sec * (attempt + 1))
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


def _sector_code(sector: dict[str, Any]) -> str:
    return str(sector.get("sector_code") or sector.get("code") or "").strip()


def _sector_name(sector: dict[str, Any]) -> str:
    return str(sector.get("sector_name") or sector.get("name") or "").strip()


def _normalize_stock_symbol(symbol: str) -> str | None:
    sym = symbol.strip().lower()
    if len(sym) < 8:
        return None
    prefix, digits = sym[:2], sym[2:]
    if prefix not in ("sh", "sz", "bj") or not digits.isdigit():
        return None
    return sym


def _sector_has_stocks(sector: dict[str, Any]) -> bool:
    stocks = sector.get("stocks")
    return isinstance(stocks, list) and len(stocks) > 0


def _normalize_sector_name(name: str) -> str:
    return (
        name.replace("Ⅱ", "")
        .replace("II", "")
        .replace("（", "(")
        .replace("）", ")")
        .strip()
    )


# ---------------------------------------------------------------------------
# 步骤一：申万二级行业列表
# ---------------------------------------------------------------------------


def _parse_shenwan_v2_from_hq_nodes(payload: list[Any]) -> list[dict[str, Any]]:
    a_share = None
    for item in payload[1]:
        if isinstance(item, list) and item and item[0] == "A股":
            a_share = item
            break
    if a_share is None:
        raise ValueError("新浪 getHQNodes 响应中未找到 A股 节点")

    sw2_node = None
    for sub in a_share[1]:
        if isinstance(sub, list) and sub and sub[0] == "申万二级":
            sw2_node = sub
            break
    if sw2_node is None:
        raise ValueError("新浪 getHQNodes 响应中未找到「申万二级」节点")

    sectors: list[dict[str, Any]] = []
    for row in sw2_node[1]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        name, code = str(row[0]).strip(), str(row[2]).strip()
        if name and code:
            sectors.append({"sector_code": code, "sector_name": name, "stocks": []})

    if not sectors:
        raise ValueError("申万二级行业列表为空")
    return sectors


def fetch_sector_stocks_from_sina(sector_code: str) -> list[dict[str, str]]:
    by_symbol: dict[str, str] = {}
    page = 1
    while page <= STOCK_MAX_PAGES:
        params = {
            "page": str(page),
            "num": str(STOCK_PAGE_SIZE),
            "sort": "symbol",
            "asc": "1",
            "node": sector_code,
            "symbol": "",
            "_s_r_a": "init",
        }

        def _fetch():
            resp = requests.get(
                SINA_HQ_NODE_DATA_URL,
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=22,
            )
            if resp.status_code in STOCK_RATE_LIMIT_STATUSES:
                raise requests.HTTPError(
                    f"新浪限流 status={resp.status_code}",
                    response=resp,
                )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"{sector_code} page {page}: 期望 list")
            return data

        rows = _with_retry(_fetch, retries=6, sleep_sec=STOCK_RATE_LIMIT_BACKOFF_SEC)
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = _normalize_stock_symbol(str(row.get("symbol", "")))
            name = row.get("name")
            if sym and isinstance(name, str) and name.strip():
                by_symbol[sym] = name.strip()
        if len(rows) < STOCK_PAGE_SIZE:
            break
        page += 1
    return [
        {"stock_code": sym, "stock_name": by_symbol[sym]}
        for sym in sorted(by_symbol.keys())
    ]


def enrich_sectors_with_stocks(
    sectors: list[dict[str, Any]],
    *,
    only_missing: bool = False,
    json_path: Path | None = None,
) -> list[dict[str, Any]]:
    total = len(sectors)
    for idx, sector in enumerate(sectors, start=1):
        code = _sector_code(sector)
        name = _sector_name(sector)
        if not code:
            raise ValueError(f"行业缺少 sector_code: {sector!r}")
        if only_missing and _sector_has_stocks(sector):
            continue
        logging.info("[%d/%d] 抓取成分股 %s (%s)…", idx, total, name, code)
        sector["sector_code"] = code
        sector["sector_name"] = name
        sector.pop("code", None)
        sector.pop("name", None)
        sector["stocks"] = fetch_sector_stocks_from_sina(code)
        logging.info("  → %d 只成分股", len(sector["stocks"]))
        if json_path is not None:
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(sectors, f, ensure_ascii=False, indent=2)
        time.sleep(STOCK_FETCH_SLEEP_SEC)
    return sectors


def _normalize_sectors_json(sectors: list[Any]) -> list[dict[str, Any]]:
    if not isinstance(sectors, list) or not sectors:
        raise ValueError("行业列表为空或格式无效")
    out: list[dict[str, Any]] = []
    for item in sectors:
        if not isinstance(item, dict):
            continue
        code, name = _sector_code(item), _sector_name(item)
        if code and name:
            out.append(
                {
                    "sector_code": code,
                    "sector_name": name,
                    "stocks": item.get("stocks") if isinstance(item.get("stocks"), list) else [],
                }
            )
    if not out:
        raise ValueError("行业列表解析后为空")
    return out


def fetch_shenwan_v2_sectors_from_sina() -> list[dict[str, Any]]:
    logging.info("正在从新浪 getHQNodes 抓取申万二级行业列表…")

    def _fetch():
        resp = requests.get(SINA_HQ_NODES_URL, headers=DEFAULT_HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json()

    payload = _with_retry(_fetch)
    if not isinstance(payload, list):
        raise ValueError("getHQNodes 返回格式异常")
    sectors = _parse_shenwan_v2_from_hq_nodes(payload)
    logging.info("抓取完成，共 %d 个申万二级行业", len(sectors))
    return sectors


def apply_stable_fetch_profile() -> None:
    """放慢请求节奏、加长限流退避（供抓取脚本 --stable 使用）。"""
    global STOCK_FETCH_SLEEP_SEC, STOCK_RATE_LIMIT_BACKOFF_SEC  # noqa: PLW0603
    STOCK_FETCH_SLEEP_SEC = STABLE_STOCK_FETCH_SLEEP_SEC
    STOCK_RATE_LIMIT_BACKOFF_SEC = STABLE_RATE_LIMIT_BACKOFF_SEC


def save_sector_codes_json(output_dir: Path, sectors: list[dict[str, Any]]) -> Path:
    """从行业列表提取 code/name，写入 shenwan_v2_sector_codes.json。"""
    codes_path = output_dir / SECTOR_CODES_JSON
    payload = [
        {"sector_code": _sector_code(s), "sector_name": _sector_name(s)}
        for s in sectors
        if _sector_code(s) and _sector_name(s)
    ]
    with codes_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logging.info("行业 code/name 已保存至 %s", codes_path)
    return codes_path


def load_sectors_json(output_dir: Path) -> list[dict[str, Any]]:
    json_path = output_dir / SECTOR_CODES_JSON
    if not json_path.is_file():
        raise FileNotFoundError(
            f"未找到 {json_path}，请先执行 ./fetch_shenwan_v2_sectors.sh 或维护行业 code/name 列表"
        )
    with json_path.open("r", encoding="utf-8") as f:
        sectors = _normalize_sectors_json(json.load(f))
    logging.info("已加载 %d 个行业（%s）", len(sectors), json_path)
    return sectors


def count_sectors_missing_stocks(sectors: list[dict[str, Any]]) -> int:
    return sum(1 for s in sectors if not _sector_has_stocks(s))


def load_or_fetch_sectors(
    output_dir: Path,
    *,
    force_refresh: bool = False,
    refresh_stocks: bool = False,
) -> list[dict[str, Any]]:
    json_path = output_dir / SECTORS_JSON
    need_list = force_refresh or not json_path.is_file()
    need_stocks = force_refresh or refresh_stocks

    if json_path.is_file() and not need_list:
        logging.info("已存在 %s，读取本地行业列表", json_path)
        with json_path.open("r", encoding="utf-8") as f:
            sectors = _normalize_sectors_json(json.load(f))
        if not need_stocks and all(_sector_has_stocks(s) for s in sectors):
            return sectors
    elif need_list:
        sectors = _normalize_sectors_json(fetch_shenwan_v2_sectors_from_sina())
    else:
        raise ValueError(f"未找到 {json_path}")

    if need_stocks or not all(_sector_has_stocks(s) for s in sectors):
        logging.info("开始抓取各行业成分股（新浪 getHQNodeData）…")
        sectors = enrich_sectors_with_stocks(
            sectors,
            only_missing=refresh_stocks and not force_refresh,
            json_path=json_path,
        )

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(sectors, f, ensure_ascii=False, indent=2)
    logging.info("行业及成分股已保存至 %s", json_path)
    save_sector_codes_json(output_dir, sectors)
    return sectors


# ---------------------------------------------------------------------------
# 801 指数代码映射（K 线 symbol 变体 + 申万官方回退）
# ---------------------------------------------------------------------------


def build_sw_index_code_map() -> dict[str, str]:
    df = ak.sw_index_second_info()
    mapping: dict[str, str] = {}
    for _, row in df.iterrows():
        code = str(row["行业代码"]).replace(".SI", "").strip()
        name = str(row["行业名称"]).strip()
        mapping[name] = code
        mapping[_normalize_sector_name(name)] = code
    return mapping


def resolve_sw_index_code(sector_name: str, code_map: dict[str, str]) -> str | None:
    return code_map.get(sector_name) or code_map.get(_normalize_sector_name(sector_name))


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------


def _parse_kline_rows(rows: list[dict[str, Any]], *, date_key: str, close_key: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_key], errors="coerce"),
            "close": pd.to_numeric(df[close_key], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if out.empty:
        raise ValueError("K 线解析后为空")
    return out


def _try_sina_kline(symbol: str, datalen: int) -> pd.DataFrame | None:
    params = {"symbol": symbol.lower(), "scale": "240", "ma": "no", "datalen": str(datalen)}
    for url in (SINA_KLINE_URL, SINA_KLINE_URL_ALT):
        try:
            resp = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=12)
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list) and rows:
                df = _parse_kline_rows(rows, date_key="day", close_key="close")
                if len(df) >= RETURN_WINDOW + 1:
                    return df.tail(datalen).reset_index(drop=True)
        except EXPECTED_BUSINESS_EXCEPTIONS:
            continue
        except Exception:
            raise
    return None


def fetch_daily_kline_from_swhy(sw_index_code: str, datalen: int = KLINE_FETCH_LEN) -> pd.DataFrame:
    params = {"swindexcode": sw_index_code, "period": "DAY"}

    def _fetch():
        resp = requests.get(
            SWHY_TREND_URL,
            params=params,
            headers=DEFAULT_HEADERS,
            timeout=20,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()

    payload = _with_retry(_fetch, retries=2)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"申万指数 {sw_index_code} 无历史数据")
    df = _parse_kline_rows(rows, date_key="bargaindate", close_key="closeindex")
    if len(df) < RETURN_WINDOW + 1:
        raise ValueError(f"申万指数 {sw_index_code} 有效 K 线不足")
    return df.tail(datalen).reset_index(drop=True)


def fetch_sector_daily_kline(
    sector_code: str,
    sector_name: str,
    sw_index_code: str | None,
    datalen: int = KLINE_FETCH_LEN,
) -> pd.DataFrame:
    """优先新浪 getKLineData，失败则申万官方 trend 接口。"""
    for sym in filter(
        None,
        [sector_code, sw_index_code, f"sh{sw_index_code}" if sw_index_code else None],
    ):
        df = _try_sina_kline(sym, datalen)
        if df is not None:
            return df

    if not sw_index_code:
        raise ValueError(f"{sector_name}({sector_code}) 无法解析 801 指数代码")
    return fetch_daily_kline_from_swhy(sw_index_code, datalen=datalen)


def fetch_benchmark_kline(datalen: int = KLINE_FETCH_LEN) -> pd.DataFrame:
    df = _try_sina_kline(BENCHMARK_SYMBOL, datalen)
    if df is None:
        raise RuntimeError(f"无法获取基准 {BENCHMARK_SYMBOL} 的新浪 K 线")
    return df.tail(TRADING_DAYS).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 打标
# ---------------------------------------------------------------------------


def _pct_return(series: pd.Series, window: int) -> float:
    if len(series) < window + 1:
        raise ValueError("样本长度不足")
    start = float(series.iloc[-(window + 1)])
    end = float(series.iloc[-1])
    if start == 0:
        raise ValueError("起始收盘价为 0")
    return (end / start - 1.0) * 100.0


def _bull_alignment(close: pd.Series) -> int:
    if len(close) < 60:
        return 0
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20) or pd.isna(ma60):
        return 0
    return 1 if ma5 > ma10 > ma20 > ma60 else 0


def analyze_sector(
    sector: dict[str, Any],
    benchmark: pd.DataFrame,
    code_map: dict[str, str],
) -> dict[str, Any] | None:
    code, name = _sector_code(sector), _sector_name(sector)
    sw_index_code = resolve_sw_index_code(name, code_map)

    try:
        kline = fetch_sector_daily_kline(code, name, sw_index_code).tail(TRADING_DAYS)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("跳过 %s (%s)：%s", name, code, exc)
        return None
    except Exception:
        logging.exception("analyze_sector: %s (%s) 未预期异常", name, code)
        raise

    merged = pd.merge(
        kline[["date", "close"]].rename(columns={"close": "sector_close"}),
        benchmark[["date", "close"]].rename(columns={"close": "bench_close"}),
        on="date",
        how="inner",
    )
    if len(merged) < max(60, RETURN_WINDOW + 1):
        logging.warning("跳过 %s (%s)：与沪深300对齐后 K 线不足", name, code)
        return None

    try:
        return_20 = _pct_return(merged["sector_close"], RETURN_WINDOW)
        bench_return_20 = _pct_return(merged["bench_close"], RETURN_WINDOW)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("跳过 %s (%s)：涨幅计算失败 — %s", name, code, exc)
        return None
    except Exception:
        logging.exception("analyze_sector: %s (%s) 涨幅计算未预期异常", name, code)
        raise

    excess = return_20 - bench_return_20
    outperform = 1 if return_20 > bench_return_20 else 0
    bull = _bull_alignment(merged["sector_close"])
    perfect = 1 if outperform == 1 and bull == 1 else 0
    data_date = merged["date"].iloc[-1].strftime("%Y-%m-%d")

    return {
        "数据日期": data_date,
        "行业代码": code,
        "行业名称": name,
        "近20日涨幅": round(return_20, 4),
        "超额收益率": round(excess, 4),
        "是否跑赢大盘": outperform,
        "是否均线多头": bull,
        "是否综合满足": perfect,
    }


def _run_sector_labeling(
    sectors: list[dict[str, Any]],
    output_dir: Path,
    *,
    workers: int = 1,
) -> Path:
    code_map = build_sw_index_code_map()

    logging.info("正在获取沪深300基准 K 线…")
    benchmark = fetch_benchmark_kline()
    logging.info("基准 %d 根，最新 %s", len(benchmark), benchmark["date"].iloc[-1].date())

    results: list[dict[str, Any]] = []
    total = len(sectors)
    workers = max(1, min(workers, total))

    if workers == 1:
        logging.info("顺序分析 %d 个行业（稳定模式）…", total)
        for idx, sector in enumerate(sectors, start=1):
            if idx % 10 == 0 or idx == total:
                logging.info("进度 %d / %d — %s", idx, total, _sector_name(sector))
            row = analyze_sector(sector, benchmark, code_map)
            if row:
                results.append(row)
            time.sleep(0.25)
    else:
        logging.info("并发分析 %d 个行业（workers=%d）…", total, workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(analyze_sector, s, benchmark, code_map): s for s in sectors}
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 25 == 0 or done == total:
                    logging.info("进度 %d / %d", done, total)
                row = fut.result()
                if row:
                    results.append(row)

    if not results:
        raise RuntimeError("无任何行业分析结果")

    df = pd.DataFrame(results)[list(ANALYSIS_RESULT_COLUMNS)]
    warn_if_sector_data_lags(benchmark, df)
    filtered = df[df["是否综合满足"] == 1].reset_index(drop=True)
    csv_path = output_dir / RESULT_CSV
    history_path = output_dir / RESULT_HISTORY_CSV
    write_snapshot_and_append_history(
        filtered,
        history_path,
        snapshot_path=csv_path,
        sort_snapshot_by="超额收益率",
    )

    logging.info("完成 %d / %d 个行业 → %s", len(filtered), total, csv_path)
    logging.info("综合满足行业数：%d", len(filtered))
    return csv_path


def run_analysis(
    output_dir: Path,
    *,
    force_refresh_sectors: bool = False,
    refresh_stocks: bool = False,
    workers: int = 1,
) -> Path:
    sectors = load_or_fetch_sectors(
        output_dir,
        force_refresh=force_refresh_sectors,
        refresh_stocks=refresh_stocks,
    )
    return _run_sector_labeling(sectors, output_dir, workers=workers)


def run_analysis_only(output_dir: Path, *, workers: int = 1) -> Path:
    """仅基于本地 shenwan_v2_sector_codes.json 的行业 code/name 打标，不抓取成分股。"""
    sectors = load_sectors_json(output_dir)
    return _run_sector_labeling(sectors, output_dir, workers=workers)


def main() -> None:
    parser = argparse.ArgumentParser(description="申万二级行业指数量化打标")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--force-refresh-sectors",
        action="store_true",
        help="强制重新抓取行业列表及成分股",
    )
    parser.add_argument(
        "--refresh-stocks",
        action="store_true",
        help="保留行业列表，仅重新抓取成分股",
    )
    parser.add_argument(
        "--sectors-only",
        action="store_true",
        help="仅更新 shenwan_v2_sectors.json，不执行量化分析",
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="仅读取本地 shenwan_v2_sector_codes.json 做行业打标，输出 CSV（不抓行业/成分股）",
    )
    parser.add_argument(
        "--stable",
        action="store_true",
        help="抓取成分股时放慢节奏、加长限流退避（偏稳定）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="打标并发数，默认 1（顺序执行，更稳）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.stable:
        apply_stable_fetch_profile()

    try:
        if args.analysis_only:
            if args.sectors_only:
                parser.error("--analysis-only 与 --sectors-only 不能同时使用")
            csv_path = run_analysis_only(output_dir, workers=args.workers)
        elif args.sectors_only:
            load_or_fetch_sectors(
                output_dir,
                force_refresh=args.force_refresh_sectors,
                refresh_stocks=args.refresh_stocks or args.force_refresh_sectors,
            )
            print(f"行业 JSON：{output_dir / SECTORS_JSON}")
            return
        else:
            csv_path = run_analysis(
                output_dir,
                force_refresh_sectors=args.force_refresh_sectors,
                refresh_stocks=args.refresh_stocks,
                workers=args.workers,
            )
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.error("执行失败：%s", exc)
        sys.exit(1)
    except Exception:
        logging.exception("执行未预期异常")
        raise

    print(f"结果文件：{csv_path}")


if __name__ == "__main__":
    main()
