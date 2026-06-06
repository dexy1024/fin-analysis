#!/usr/bin/env python3
"""
抓取 510300（华泰柏瑞沪深300ETF）期权平值/虚一档持仓与认沽隐含波动率，并导出标的成交量。

数据源：
  - 上交所 yunhq T 型报价：行权价、持仓量（open_interest）、成交量
  - 上交所期权风险指标：认沽隐含波动率（IMPLC_VOLATLTY）
  - 新浪标的现货：510300 当日成交量
  - 东财 fund_etf_hist_em：近 5 个交易日成交量（亦写入 11_510300_etf_volume.csv）
  - 成交量比：标的当日成交量 / 前 5 个交易日均量（不含当日），挂到 10_510300_option_oi_iv.csv
  - 开空条件（四项同时满足）：收盘 < MA30、认沽 IV >= 25%、PCR > 1、成交量比 > 1

输出（默认项目根目录）：
  - 10_510300_option_oi_iv.csv（含成交量比、开空条件信号）
  - 11_510300_etf_volume.csv

用法：
  cd backend && python3 scripts/fetch_510300_option_snapshot.py
  cd backend && python3 scripts/fetch_510300_option_snapshot.py -o .. --end-month 2606,2607
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import requests

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from services.option_quote import ETF_OPTION_REGISTRY  # noqa: E402
from utils.expected_exceptions import EXPECTED_BUSINESS_EXCEPTIONS  # noqa: E402

OPTION_CSV = "10_510300_option_oi_iv.csv"
VOLUME_CSV = "11_510300_etf_volume.csv"

MA_WINDOW = 30
SHORT_IV_MIN = 0.25
SHORT_PCR_MIN = 1.0
SHORT_VOL_RATIO_MIN = 1.0

SSE_BOARD_SELECT = (
    "contractid,last,chg_rate,presetpx,exepx,open,high,low,volume,amount,"
    "openinterest,position,open_interest,oi,impliedvolatility,iv"
)
SSE_KING_URL = "http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/510300_{month}"


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


def _default_end_months() -> list[str]:
    """默认拉取当月 + 次月（如 2606、2607）。"""
    cur = datetime.now().replace(day=1)
    ym1 = cur.strftime("%y%m")
    next_month = (cur + pd.Timedelta(days=32)).replace(day=1)
    ym2 = next_month.strftime("%y%m")
    return sorted({ym1, ym2})


def _parse_end_months(end_month: str | None) -> list[str]:
    if not end_month or not str(end_month).strip():
        return _default_end_months()
    return [_normalize_end_month(p.strip()) for p in str(end_month).split(",") if p.strip()]


def _normalize_end_month(end_month: str | None) -> str:
    ym = (end_month or _default_end_month()).strip()
    if not re.fullmatch(r"\d{4}", ym):
        raise ValueError("end_month 须为 YYMM 格式，例如 2606")
    return ym


def _to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _option_type_from_code(contract_code: str) -> str:
    """上交所合约：510300C2606M04800 / 510300P2606A04300。"""
    upper = contract_code.upper()
    m = re.match(r"^\d{6}([CP])", upper)
    if m and m.group(1) == "P":
        return "put"
    return "call"


def _is_m_series_contract(contract_code: str) -> bool:
    """M 标准档（如 510300C2606M04800）；A 档为除权调整合约（如 510300C2606A05000）。"""
    return bool(re.search(r"\d{4}M\d", contract_code.upper()))


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
        "volume": _to_float(kv.get("成交数量")),
        "amount": _to_float(kv.get("成交金额")),
        "quote_date": kv.get("行情日期"),
        "quote_time": kv.get("行情时间"),
    }


def _fetch_sse_board_extended(end_month: str) -> tuple[pd.DataFrame, str | None]:
    """上交所 T 型报价（含持仓量 open_interest）。"""
    month = end_month[-2:]

    def _call() -> dict[str, Any]:
        resp = requests.get(
            SSE_KING_URL.format(month=month),
            params={"select": SSE_BOARD_SELECT},
            timeout=12,
        )
        resp.raise_for_status()
        return resp.json()

    data = _with_retry(_call)
    rows = data.get("list") or []
    if not rows:
        return pd.DataFrame(), None

    cols = SSE_BOARD_SELECT.split(",")
    records: list[dict[str, Any]] = []
    for item in rows:
        row = dict(zip(cols, item))
        code = str(row["contractid"])
        records.append(
            {
                "contract_code": code,
                "contract_series": "M" if _is_m_series_contract(code) else "A",
                "option_type": _option_type_from_code(code),
                "strike": _to_float(row.get("exepx")),
                "last_price": _to_float(row.get("last")),
                "volume": _to_float(row.get("volume")),
                "open_interest": _to_float(row.get("open_interest")),
                "quote_time": f"{data.get('date', '')}{data.get('time', '')}",
            }
        )
    trade_date = str(data.get("date")) if data.get("date") else None
    return pd.DataFrame(records), trade_date


def _fetch_put_iv_by_contract(trade_date: str) -> dict[str, float]:
    """合约代码 -> 认沽隐含波动率。"""
    ri = _with_retry(lambda: ak.option_risk_indicator_sse(date=trade_date))
    out: dict[str, float] = {}
    for _, row in ri.iterrows():
        code = str(row.get("CONTRACT_ID", ""))
        if not code.startswith("510300P"):
            continue
        iv = _to_float(row.get("IMPLC_VOLATLTY"))
        if iv is not None:
            out[code] = iv
    return out


def _pick_atm_and_otm_put_strikes(strikes: list[float], spot: float) -> tuple[float, float]:
    """平值行权价 + 虚一档认沽行权价（平值下方相邻一档）。"""
    uniq = sorted(set(strikes))
    if not uniq:
        raise ValueError("期权行权价列表为空")
    atm = min(uniq, key=lambda s: abs(s - spot))
    below = [s for s in uniq if s < atm]
    if not below:
        raise ValueError(f"平值行权价 {atm} 下方无可用档位，无法计算虚一档认沽")
    otm_put = max(below)
    return atm, otm_put


def _contract_at_strike(df: pd.DataFrame, strike: float, option_type: str) -> pd.Series | None:
    sub = df[(df["strike"] == strike) & (df["option_type"] == option_type)]
    if sub.empty:
        return None
    return sub.iloc[0]


def _calc_etf_volume_ratio(volume_df: pd.DataFrame) -> dict[str, Any]:
    """当日成交量 / 前 5 个交易日成交量均值（不含当日）。"""
    df = volume_df.copy()
    df["交易日期"] = pd.to_datetime(df["交易日期"], errors="coerce")
    df = df.dropna(subset=["交易日期", "成交量"]).sort_values("交易日期")
    df = df.drop_duplicates(subset=["交易日期"], keep="first")
    if df.empty:
        raise ValueError("成交量数据为空")

    today_row = df.iloc[-1]
    prior = df.iloc[:-1].tail(5)
    if len(prior) < 5:
        raise ValueError(f"前 5 交易日成交量不足（仅 {len(prior)} 天）")

    today_vol = float(today_row["成交量"])
    avg_5 = float(prior["成交量"].mean())
    ratio: float | None = None
    if avg_5 > 0:
        ratio = round(today_vol / avg_5, 2)

    return {
        "标的当日成交量": int(today_vol),
        "标的前五日均成交量": int(round(avg_5)),
        "成交量比": ratio,
    }


def _build_option_rows(
    board: pd.DataFrame,
    spot: float,
    put_iv_map: dict[str, float],
    *,
    trade_date: str,
    end_month: str,
    underlying_spot: dict[str, Any],
    volume_metrics: dict[str, Any],
) -> pd.DataFrame:
    # 行情 App 平值/虚一档按 M 标准档（4.8/4.9/5.0），非 A 除权调整档（4.873 等）
    m_board = board[board["contract_series"] == "M"].copy()
    if m_board.empty:
        raise ValueError("未找到 M 标准档合约，无法计算平值/虚一档")
    strikes = [s for s in m_board["strike"].dropna().tolist() if s is not None]
    atm_strike, otm_put_strike = _pick_atm_and_otm_put_strikes(strikes, spot)

    rows: list[dict[str, Any]] = []
    for label, strike in (("平值", atm_strike), ("虚一档认沽", otm_put_strike)):
        call_row = _contract_at_strike(m_board, strike, "call")
        put_row = _contract_at_strike(m_board, strike, "put")
        put_code = str(put_row["contract_code"]) if put_row is not None else ""
        call_oi = call_row["open_interest"] if call_row is not None else None
        put_oi = put_row["open_interest"] if put_row is not None else None
        pcr: float | None = None
        if call_oi and put_oi is not None and call_oi > 0:
            pcr = round(float(put_oi) / float(call_oi), 2)
        rows.append(
            {
                "数据日期": trade_date,
                "到期月份": end_month,
                "标的代码": "510300",
                "标的名称": underlying_spot.get("name"),
                "标的现价": spot,
                "标的当日成交量": volume_metrics.get("标的当日成交量"),
                "标的前五日均成交量": volume_metrics.get("标的前五日均成交量"),
                "成交量比": volume_metrics.get("成交量比"),
                "档位": label,
                "行权价": strike,
                "认购合约代码": call_row["contract_code"] if call_row is not None else None,
                "认沽合约代码": put_code or None,
                "认购持仓量": call_oi,
                "认沽持仓量": put_oi,
                "PCR": pcr,
                "认沽隐含波动率": put_iv_map.get(put_code),
                "认购最新价": call_row["last_price"] if call_row is not None else None,
                "认沽最新价": put_row["last_price"] if put_row is not None else None,
                "认购成交量": call_row["volume"] if call_row is not None else None,
                "认沽成交量": put_row["volume"] if put_row is not None else None,
            }
        )
    return pd.DataFrame(rows)


def _fetch_etf_daily_from_sina(code: str, datalen: int = 96) -> pd.DataFrame:
    symbol = f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )

    def _call() -> pd.DataFrame:
        resp = requests.get(
            url,
            params={"symbol": symbol.lower(), "scale": "240", "ma": "no", "datalen": str(datalen)},
            timeout=12,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"新浪未返回 {symbol} 日线")
        df = pd.DataFrame(rows)
        return pd.DataFrame(
            {
                "交易日期": pd.to_datetime(df["day"], errors="coerce"),
                "收盘价": pd.to_numeric(df["close"], errors="coerce"),
                "成交量": pd.to_numeric(df["volume"], errors="coerce"),
            }
        ).dropna(subset=["交易日期", "收盘价"])

    return _with_retry(_call)


def _load_etf_daily_from_local_cache(code: str) -> pd.DataFrame:
    """读取 backend/data/a_daily_nq_{code}.csv 本地日线缓存。"""
    cache_path = _BACKEND_DIR / "data" / f"a_daily_nq_{code}.csv"
    if not cache_path.is_file():
        raise FileNotFoundError(f"本地日线缓存不存在: {cache_path}")
    raw = pd.read_csv(cache_path)
    if raw.empty or "date" not in raw.columns or "close" not in raw.columns:
        raise ValueError(f"本地日线缓存格式无效: {cache_path}")
    out = pd.DataFrame(
        {
            "交易日期": pd.to_datetime(raw["date"], errors="coerce"),
            "收盘价": pd.to_numeric(raw["close"], errors="coerce"),
            "成交量": pd.to_numeric(raw.get("volume"), errors="coerce"),
        }
    ).dropna(subset=["交易日期", "收盘价"])
    if out.empty:
        raise ValueError(f"本地日线缓存无有效数据: {cache_path}")
    return out


def _fetch_etf_daily_closes(code: str, min_bars: int = MA_WINDOW) -> pd.DataFrame:
    end_s = datetime.now().strftime("%Y%m%d")
    start_s = (datetime.now() - pd.Timedelta(days=min_bars + 30)).strftime("%Y%m%d")

    def _call_em() -> pd.DataFrame:
        raw = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="",
        )
        if raw is None or raw.empty:
            raise ValueError(f"未获取到 {code} 东财日线")
        return pd.DataFrame(
            {
                "交易日期": pd.to_datetime(raw["日期"], errors="coerce"),
                "收盘价": pd.to_numeric(raw["收盘"], errors="coerce"),
                "成交量": pd.to_numeric(raw["成交量"], errors="coerce"),
            }
        ).dropna(subset=["交易日期", "收盘价"])

    try:
        return _with_retry(_call_em, retries=2)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("东财 ETF 日线不可用，尝试新浪: %s", exc)
    try:
        return _fetch_etf_daily_from_sina(code)
    except EXPECTED_BUSINESS_EXCEPTIONS as exc:
        logging.warning("新浪 ETF 日线不可用，改用本地缓存: %s", exc)
        return _load_etf_daily_from_local_cache(code)


def _calc_ma30_metrics(daily_df: pd.DataFrame, today_close: float) -> dict[str, Any]:
    df = daily_df.sort_values("交易日期").drop_duplicates(subset=["交易日期"], keep="last")
    if len(df) < MA_WINDOW:
        raise ValueError(f"计算 MA{MA_WINDOW} 至少需要 {MA_WINDOW} 根日线（当前 {len(df)}）")
    ma30 = float(df["收盘价"].tail(MA_WINDOW).mean())
    return {
        "标的收盘价": round(today_close, 4),
        "MA30": round(ma30, 4),
        "收盘低于30日线": today_close < ma30,
    }


def _bool_cn(flag: bool) -> str:
    return "是" if flag else "否"


def _apply_short_entry_signals(option_df: pd.DataFrame, ma_metrics: dict[str, Any]) -> pd.DataFrame:
    """开空四项：收盘<MA30、IV>=25%、PCR>1、成交量比>1。"""
    df = option_df.copy()
    below_ma30 = bool(ma_metrics["收盘低于30日线"])

    df.insert(df.columns.get_loc("标的现价") + 1, "标的收盘价", ma_metrics["标的收盘价"])
    df.insert(df.columns.get_loc("标的收盘价") + 1, "MA30", ma_metrics["MA30"])

    iv_ok = df["认沽隐含波动率"].notna() & (df["认沽隐含波动率"] >= SHORT_IV_MIN)
    pcr_ok = df["PCR"].notna() & (df["PCR"] > SHORT_PCR_MIN)
    vol_ok = df["成交量比"].notna() & (df["成交量比"] > SHORT_VOL_RATIO_MIN)

    df["收盘低于30日线"] = _bool_cn(below_ma30)
    df["波动率达标"] = iv_ok.map(_bool_cn)
    df["PCR达标"] = pcr_ok.map(_bool_cn)
    df["成交量比达标"] = vol_ok.map(_bool_cn)
    df["开空满足"] = (below_ma30 & iv_ok & pcr_ok & vol_ok).map(_bool_cn)
    return df


def _build_volume_df(
    code: str,
    daily_df: pd.DataFrame,
    underlying_spot: dict[str, Any],
    *,
    tail_n: int = 8,
) -> pd.DataFrame:
    """由日线 + 当日现货拼成交量表（写入 11_510300_etf_volume.csv）。"""
    hist = daily_df.copy()
    hist["交易日期"] = pd.to_datetime(hist["交易日期"])
    hist = hist.sort_values("交易日期").tail(tail_n)
    hist["交易日期"] = hist["交易日期"].dt.strftime("%Y-%m-%d")
    hist.insert(0, "标的代码", code)
    hist["成交额"] = None
    if "数据来源" not in hist.columns:
        hist["数据来源"] = "本地/新浪日线"

    today_row = pd.DataFrame(
        [
            {
                "标的代码": code,
                "交易日期": underlying_spot.get("quote_date"),
                "收盘价": underlying_spot.get("last_price"),
                "成交量": underlying_spot.get("volume"),
                "成交额": underlying_spot.get("amount"),
                "数据来源": "新浪现货实时",
            }
        ]
    )
    volume_df = pd.concat([today_row, hist], ignore_index=True)
    volume_df.drop_duplicates(subset=["交易日期"], keep="first", inplace=True)
    volume_df.sort_values("交易日期", inplace=True)
    volume_df.reset_index(drop=True, inplace=True)
    return volume_df


def fetch_snapshot(
    underlying: str = "510300",
    *,
    end_months: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    meta = ETF_OPTION_REGISTRY.get(underlying)
    if meta is None or meta.get("exchange") != "sse":
        raise ValueError(f"当前脚本仅支持上交所 ETF 期权，收到: {underlying}")

    months = end_months or _default_end_months()
    underlying_spot = _fetch_underlying_spot(meta["etf_sina"])
    spot = underlying_spot.get("last_price")
    if spot is None:
        raise ValueError("无法获取 510300 现价")

    daily_closes = _fetch_etf_daily_closes(underlying)
    volume_df = _build_volume_df(underlying, daily_closes, underlying_spot, tail_n=8)
    volume_metrics = _calc_etf_volume_ratio(volume_df)

    today_close = float(volume_df.iloc[-1]["收盘价"])
    today_ts = pd.to_datetime(volume_df.iloc[-1]["交易日期"])
    daily_for_ma = daily_closes[daily_closes["交易日期"] != today_ts]
    daily_for_ma = pd.concat(
        [
            daily_for_ma,
            pd.DataFrame({"交易日期": [today_ts], "收盘价": [today_close]}),
        ],
        ignore_index=True,
    )
    ma_metrics = _calc_ma30_metrics(daily_for_ma, today_close)

    trade_date = datetime.now().strftime("%Y%m%d")
    put_iv_map: dict[str, float] = {}
    option_frames: list[pd.DataFrame] = []
    for ym in months:
        board, trade_date_raw = _fetch_sse_board_extended(ym)
        if board.empty:
            logging.warning("跳过无行情到期月: %s", ym)
            continue
        if trade_date_raw:
            trade_date = trade_date_raw
        if not put_iv_map:
            put_iv_map = _fetch_put_iv_by_contract(trade_date)
        month_df = _build_option_rows(
            board,
            spot,
            put_iv_map,
            trade_date=trade_date,
            end_month=ym,
            underlying_spot=underlying_spot,
            volume_metrics=volume_metrics,
        )
        option_frames.append(_apply_short_entry_signals(month_df, ma_metrics))

    if not option_frames:
        raise ValueError(f"未获取到 {underlying} 期权行情，到期月: {months}")

    option_df = pd.concat(option_frames, ignore_index=True)
    return option_df, volume_df


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="导出 510300 期权平值/虚一档持仓与成交量 CSV")
    parser.add_argument("--underlying", default="510300", help="标的 ETF 代码，默认 510300")
    parser.add_argument(
        "--end-month",
        default=None,
        help="到期月份 YYMM，逗号分隔；默认当月+次月（如 2606,2607）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=str(_BACKEND_DIR.parent),
        help="CSV 输出目录，默认项目根目录",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    option_df, volume_df = fetch_snapshot(
        args.underlying,
        end_months=_parse_end_months(args.end_month),
    )

    option_path = out_dir / OPTION_CSV
    volume_path = out_dir / VOLUME_CSV
    option_df.to_csv(option_path, index=False, encoding="utf-8-sig")
    volume_df.to_csv(volume_path, index=False, encoding="utf-8-sig")

    logging.info("期权快照 -> %s", option_path)
    logging.info("标的成交量 -> %s", volume_path)
    print(option_df.to_string(index=False))
    print()
    print(volume_df.to_string(index=False))


if __name__ == "__main__":
    main()
