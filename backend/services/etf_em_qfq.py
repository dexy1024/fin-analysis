"""
场内 ETF 中，新浪分钟/日线无可用前复权，除权/拆分后会出现价格断崖。

本模块维护「必须走东财 fund_etf_hist*_em adjust=qfq」的六位代码白名单；
其余 ETF 仍用新浪（与 A 股同步策略一致）。
"""

from __future__ import annotations

# 已确认除权/拆分、缠论需前复权的 ETF；有新标的时追加六位代码即可
ETF_EM_QFQ_CODES: frozenset[str] = frozenset({"515050"})


def _is_likely_etf_code(code: str) -> bool:
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("51", "56", "58", "159"))


def normalize_a_share_code(code: str) -> str:
    s = code.strip().lower()
    if s.startswith(("sh", "sz")) and len(s) >= 8:
        return s[2:8]
    return s[:6] if len(s) >= 6 else s


def etf_needs_em_qfq(code: str) -> bool:
    """是否为需东财前复权拉取的场内 ETF。"""
    c = normalize_a_share_code(code)
    return _is_likely_etf_code(c) and c in ETF_EM_QFQ_CODES
