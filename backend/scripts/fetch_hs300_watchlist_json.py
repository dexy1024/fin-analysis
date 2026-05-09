#!/usr/bin/env python3
"""
从新浪沪深300节点拉取成份 code + name，写入 backend/data/watchlist_hs300.json
（结构与 backend/data/watchlist.json 一致：仅 holdings 数组每项含 code、name）。

接口：vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData ，node=hs300 。

用法（在仓库根目录）:
    python3 backend/scripts/fetch_hs300_watchlist_json.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_PATH = os.path.join(backend_dir, "data", "watchlist_hs300.json")
URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

PAGE_SIZE = 100
NETWORK_RETRIES = 4
NETWORK_SLEEP_SEC = 0.9


def _with_retry(fetch):
    last = None
    for i in range(NETWORK_RETRIES):
        try:
            return fetch()
        except (requests.RequestException, OSError, ValueError) as e:
            last = e
            if i < NETWORK_RETRIES - 1:
                time.sleep(NETWORK_SLEEP_SEC * (i + 1))
    assert last is not None
    raise last


def _normalize_code(symbol: str, code_field: object) -> str | None:
    """与 watchlist.json 对齐：六位数字或其它纯代码（无 sh/sz 前缀）。"""
    if isinstance(code_field, str):
        s = code_field.strip()
        if s.isdigit():
            return s
        if len(s) == 6 and s.isdigit():
            return s
    if isinstance(symbol, str):
        sym = symbol.lower().strip()
        for p in ("sh", "sz"):
            if sym.startswith(p) and sym[2:].isdigit():
                return sym[2:]
    return None


def fetch_hs300_holdings() -> list[dict[str, str]]:
    holdings: dict[str, str] = {}
    page = 1
    while True:
        params = {
            "page": str(page),
            "num": str(PAGE_SIZE),
            "sort": "symbol",
            "asc": "1",
            "node": "hs300",
            "symbol": "",
            "_s_r_a": "sort",
        }

        def fetch():
            r = requests.get(URL, params=params, headers=SINA_HEADERS, timeout=22)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError(f"hs300 page {page}: 期望 list，实为 {type(data)}")
            return data

        rows = _with_retry(fetch)

        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            code = _normalize_code(str(row.get("symbol", "")), row.get("code"))
            name = row.get("name")
            if not code or not isinstance(name, str) or not name.strip():
                continue
            holdings[code] = name.strip()

        if len(rows) < PAGE_SIZE:
            break
        page += 1
        if page > 20:
            raise RuntimeError("hs300 分页超过 20 页，中止以防异常循环")

    out = [{"code": c, "name": holdings[c]} for c in sorted(holdings.keys())]
    return out


def main() -> int:
    holdings = fetch_hs300_holdings()
    n = len(holdings)
    if n != 300:
        print(f"警告: 期望 300 只成份，实际 {n} 只（仍写入，请检查网络或新浪接口）", file=sys.stderr)

    payload = {
        "_comment": (
            "沪深300成份股名单（仅 code、name）。数据来源：新浪财经 "
            "Market_Center.getHQNodeData ，node=hs300 。勿手改 holdings；"
            "刷新请运行：python3 backend/scripts/fetch_hs300_watchlist_json.py"
        ),
        "holdings": holdings,
    }

    tmp = OUT_PATH + ".tmp"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, OUT_PATH)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    print(f"已写入 {OUT_PATH} ，共 {n} 条（UTC {ts}）")
    return 0 if n == 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
