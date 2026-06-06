"""
ETF 期权 T 型报价（认购/认沽、行权价、现价等）。

数据源（akshare）：
- 上交所：option_finance_board（含实时现价）
- 深交所：option_current_day_szse（合约清单 + 前结算价；现价依赖东财接口，失败时降级）
- 标的 ETF 现价：option_sse_underlying_spot_price_sina（sh/sz 前缀）
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Literal

import akshare as ak
import pandas as pd

from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS

OptionTypeFilter = Literal["all", "put", "call"]

# 标的 ETF 代码 -> 期权配置（与 observation.json 中 588000/159915 对齐）
ETF_OPTION_REGISTRY: dict[str, dict[str, str]] = {
    "588000": {
        "name": "科创50ETF",
        "exchange": "sse",
        "board_symbol": "华夏科创50ETF期权",
        "etf_sina": "sh588000",
    },
    "588080": {
        "name": "科创50ETF易方达",
        "exchange": "sse",
        "board_symbol": "易方达科创50ETF期权",
        "etf_sina": "sh588080",
    },
    "510050": {
        "name": "上证50ETF",
        "exchange": "sse",
        "board_symbol": "华夏上证50ETF期权",
        "etf_sina": "sh510050",
    },
    "510300": {
        "name": "沪深300ETF",
        "exchange": "sse",
        "board_symbol": "华泰柏瑞沪深300ETF期权",
        "etf_sina": "sh510300",
    },
    "510500": {
        "name": "中证500ETF",
        "exchange": "sse",
        "board_symbol": "南方中证500ETF期权",
        "etf_sina": "sh510500",
    },
    "159915": {
        "name": "创业板ETF",
        "exchange": "szse",
        "contract_prefix": "159915",
        "etf_sina": "sz159915",
    },
    "159919": {
        "name": "沪深300ETF",
        "exchange": "szse",
        "contract_prefix": "159919",
        "etf_sina": "sz159919",
    },
    "159922": {
        "name": "中证500ETF",
        "exchange": "szse",
        "contract_prefix": "159922",
        "etf_sina": "sz159922",
    },
}

_MONTH_IN_CODE = re.compile(r"[CP](\d{4})M")


def list_supported_underlyings() -> list[dict[str, str]]:
    return [
        {
            "underlying": code,
            "name": meta["name"],
            "exchange": meta["exchange"],
        }
        for code, meta in sorted(ETF_OPTION_REGISTRY.items())
    ]


def _with_retry(fetch_fn, *, retries: int = 3, sleep_sec: float = 0.6):
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            return fetch_fn()
        except EXPECTED_BUSINESS_EXCEPTIONS as exc:
            last_exc = exc
            if i < retries - 1:
                time.sleep(sleep_sec * (i + 1))
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


def _default_end_month() -> str:
    return datetime.now().strftime("%y%m")


def _normalize_end_month(end_month: str | None) -> str:
    ym = (end_month or _default_end_month()).strip()
    if not re.fullmatch(r"\d{4}", ym):
        raise ValueError("end_month 须为 YYMM 格式，例如 2606")
    return ym


def _option_type_from_code(contract_code: str) -> str:
    upper = contract_code.upper()
    if "P" in upper and upper.index("P") < upper.index("M", upper.index("P")):
        return "put"
    return "call"


def _contract_matches_month(contract_code: str, end_month: str) -> bool:
    m = _MONTH_IN_CODE.search(contract_code.upper())
    return bool(m and m.group(1) == end_month)


def _to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_underlying_spot(etf_sina: str) -> dict[str, Any]:
    df = _with_retry(lambda: ak.option_sse_underlying_spot_price_sina(symbol=etf_sina))
    if df is None or df.empty or "字段" not in df.columns:
        raise ValueError(f"无法获取标的行情: {etf_sina}")
    kv = dict(zip(df["字段"].astype(str), df["值"].astype(str)))
    if kv.get("证券简称") == "FAILED":
        raise ValueError(f"新浪标的行情无效: {etf_sina}")
    return {
        "name": kv.get("证券简称"),
        "last_price": _to_float(kv.get("最近成交价")),
        "prev_close": _to_float(kv.get("昨日收盘价")),
        "open": _to_float(kv.get("今日开盘价")),
        "high": _to_float(kv.get("最高成交价")),
        "low": _to_float(kv.get("最低成交价")),
        "quote_date": kv.get("行情日期"),
        "quote_time": kv.get("行情时间"),
    }


def _normalize_sse_row(row: pd.Series) -> dict[str, Any]:
    code = str(row["合约交易代码"])
    return {
        "contract_code": code,
        "option_type": _option_type_from_code(code),
        "strike": _to_float(row.get("行权价")),
        "last_price": _to_float(row.get("当前价")),
        "change_pct": _to_float(row.get("涨跌幅")),
        "prev_settle": _to_float(row.get("前结价")),
        "volume": int(row["数量"]) if pd.notna(row.get("数量")) else None,
        "quote_time": str(row.get("日期", "")),
        "quote_source": "sse_live",
    }


def _fetch_sse_board(board_symbol: str, end_month: str) -> tuple[list[dict[str, Any]], str | None]:
    df = _with_retry(lambda: ak.option_finance_board(symbol=board_symbol, end_month=end_month))
    if df is None or df.empty:
        return [], None
    as_of = str(df.iloc[0].get("日期", "")) if "日期" in df.columns else None
    contracts = [_normalize_sse_row(row) for _, row in df.iterrows()]
    return contracts, as_of


def _normalize_szse_row(row: pd.Series) -> dict[str, Any]:
    code = str(row["合约代码"])
    return {
        "contract_code": code,
        "contract_id": int(row["合约编码"]) if pd.notna(row.get("合约编码")) else None,
        "option_type": _option_type_from_code(code),
        "strike": _to_float(row.get("行权价")),
        "last_price": None,
        "change_pct": None,
        "prev_settle": _to_float(row.get("前结算价")),
        "limit_up": _to_float(row.get("涨停价格")),
        "limit_down": _to_float(row.get("跌停价格")),
        "open_interest": _to_float(row.get("合约总持仓")),
        "expiry_date": str(row.get("到期日", "")),
        "quote_source": "szse_contract",
    }


def _try_enrich_szse_from_em(contracts: list[dict[str, Any]]) -> None:
    """东财期权现价/IV（可选）；失败时静默保留 prev_settle。"""
    try:
        df = _with_retry(lambda: ak.option_value_analysis_em(), retries=2)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("option_value_analysis_em 不可用，深交所合约无实时现价: %s", exc)
        return
    if df is None or df.empty:
        return
    code_col = "期权代码" if "期权代码" in df.columns else None
    if not code_col:
        return
    em_by_code = {str(r[code_col]): r for _, r in df.iterrows()}
    for c in contracts:
        cid = c.get("contract_id")
        if cid is None:
            continue
        row = em_by_code.get(str(cid))
        if row is None:
            continue
        c["last_price"] = _to_float(row.get("最新价"))
        c["implied_volatility"] = _to_float(row.get("隐含波动率"))
        c["intrinsic_value"] = _to_float(row.get("内在价值"))
        c["time_value"] = _to_float(row.get("时间价值"))
        c["quote_source"] = "szse_em"


def _fetch_szse_board(contract_prefix: str, end_month: str) -> tuple[list[dict[str, Any]], str | None]:
    df = _with_retry(lambda: ak.option_current_day_szse())
    if df is None or df.empty:
        return [], None
    mask = df["合约代码"].astype(str).str.startswith(contract_prefix)
    month_mask = df["合约代码"].astype(str).apply(lambda c: _contract_matches_month(c, end_month))
    sub = df[mask & month_mask]
    if sub.empty:
        return [], str(df.iloc[0].get("交易日期", "")) if "交易日期" in df.columns else None
    contracts = [_normalize_szse_row(row) for _, row in sub.iterrows()]
    _try_enrich_szse_from_em(contracts)
    as_of = str(sub.iloc[0].get("交易日期", ""))
    return contracts, as_of


def _filter_by_option_type(
    contracts: list[dict[str, Any]],
    option_type: OptionTypeFilter,
) -> list[dict[str, Any]]:
    if option_type == "all":
        return contracts
    return [c for c in contracts if c.get("option_type") == option_type]


def get_option_board(
    underlying: str,
    *,
    end_month: str | None = None,
    option_type: OptionTypeFilter = "all",
) -> dict[str, Any]:
    code = underlying.strip()
    meta = ETF_OPTION_REGISTRY.get(code)
    if meta is None:
        supported = ", ".join(sorted(ETF_OPTION_REGISTRY))
        raise ValueError(f"不支持的期权标的 {code}，可选: {supported}")

    ym = _normalize_end_month(end_month)
    exchange = meta["exchange"]

    if exchange == "sse":
        contracts, as_of = _fetch_sse_board(meta["board_symbol"], ym)
    else:
        contracts, as_of = _fetch_szse_board(meta["contract_prefix"], ym)

    contracts = _filter_by_option_type(contracts, option_type)
    contracts.sort(key=lambda c: (c.get("option_type") or "", c.get("strike") or 0.0))

    underlying_spot: dict[str, Any] | None = None
    try:
        underlying_spot = _fetch_underlying_spot(meta["etf_sina"])
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("标的 ETF 现价获取失败 %s: %s", code, exc)

    puts = sum(1 for c in contracts if c.get("option_type") == "put")
    calls = sum(1 for c in contracts if c.get("option_type") == "call")

    return {
        "underlying": code,
        "underlying_name": meta["name"],
        "exchange": exchange,
        "end_month": ym,
        "as_of": as_of,
        "underlying_spot": underlying_spot,
        "stats": {"total": len(contracts), "puts": puts, "calls": calls},
        "contracts": contracts,
        "data_notes": (
            "上交所合约含实时现价；深交所默认前结算价，东财可用时补充最新价/IV。"
            if exchange == "szse"
            else "上交所 option_finance_board 实时行情。"
        ),
    }
