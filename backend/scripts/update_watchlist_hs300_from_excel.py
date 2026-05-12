#!/usr/bin/env python3
"""
从 Excel 读取沪深300成份（代码 + 名称），写入 backend/data/watchlist_hs300.json
（排版与 fetch_hs300_watchlist_json.py 一致）。

依赖：pandas、openpyxl（.xlsx）；部分 .xls 由 pandas 自带引擎可读。在仓库根目录执行：

    python3 backend/scripts/update_watchlist_hs300_from_excel.py path/to/hs300.xlsx
    python3 backend/scripts/update_watchlist_hs300_from_excel.py 000300cons.xls

追加合并（保留已有 code+name，仅加入 Excel 中尚未出现的代码；同代码以已有为准）：

    python3 backend/scripts/update_watchlist_hs300_from_excel.py 000905cons.xls --merge-existing

可选参数：
    --out PATH          输出 JSON（默认 backend/data/watchlist_hs300.json）
    --sheet NAME|N      工作表名或 0 起索引（默认 0）
    --code-col COL      代码列表头（与表头一致，忽略首尾空格）；不指定则自动识别
    --name-col COL      名称列表头；不指定则自动识别
    --merge-existing    与 --out 指向的 JSON 已有 holdings 合并（按 code 去重，已有优先）
    --strict-300        若行数不是 300 则退出码 1（仍写入文件；与 --merge-existing 并用时一般不适用）

列名自动识别：优先「成份券代码 / Constituent Code」与「成份券名称 / Constituent Name」（中证指数
表）；否则匹配常见「证券代码 / 简称」等表头，并避免将「指数代码」误当作股票代码列。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OUT = os.path.join(backend_dir, "data", "watchlist_hs300.json")

# 表头子串匹配（小写）
_CODE_HEADER_HINTS = (
    "code",
    "symbol",
    "代码",
    "证券代码",
    "股票代码",
    "成份代码",
    "成分代码",
    "wind代码",
)
_NAME_HEADER_HINTS = (
    "name",
    "名称",
    "简称",
    "证券简称",
    "股票简称",
    "证券名称",
    "股票名称",
    "成份股名称",
    "成分股名称",
)


def _normalize_header(s: object) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip()


def _is_constituent_stock_code_header(norm: str) -> bool:
    """中证等指数成份表：优先「成份券代码 / Constituent Code」，避免误判「指数代码」。"""
    low = norm.lower()
    if "constituent code" in low:
        return True
    if ("成份券" in norm or "成分券" in norm) and "代码" in norm:
        return True
    return False


def _is_constituent_stock_name_header(norm: str) -> bool:
    low = norm.lower()
    if "(eng)" in low or "name(eng)" in low or "英文名称" in norm:
        return False
    if "constituent name" in low:
        return True
    if "成份券名称" in norm or "成分券名称" in norm:
        return True
    return False


def _is_index_meta_code_header(norm: str) -> bool:
    low = norm.lower()
    return "指数代码" in norm or "index code" in low


def _cell_to_code_str(v: object) -> str | None:
    """输出六位数字代码，与 watchlist 中格式一致。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if float(v).is_integer():
            s = str(int(v))
        else:
            s = str(v).strip()
    else:
        s = str(v).strip()
    if not s:
        return None
    s = s.replace(".0", "") if s.endswith(".0") and s[:-2].isdigit() else s
    low = s.lower()
    for p in ("sh", "sz", "bj"):
        if low.startswith(p) and len(low) >= 8 and low[2:8].isdigit():
            return low[2:8]
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 6:
        tail = digits[-6:]
        if tail.isdigit():
            return tail.zfill(6)
    if s.isdigit() and len(s) <= 6:
        return s.zfill(6)
    return None


def _pick_columns(
    df: pd.DataFrame, code_col: str | None, name_col: str | None
) -> tuple[str, str]:
    """返回 (原始列名, 原始列名)，供 df[col] 索引。"""
    raw_cols = list(df.columns)

    def resolve(user: str | None) -> str | None:
        if not user:
            return None
        u = user.strip()
        for raw in raw_cols:
            if _normalize_header(raw) == u or str(raw).strip() == u:
                return raw
        raise SystemExit(f"找不到列「{user}」。当前表头: {df.columns.tolist()}")

    c_explicit = resolve(code_col)
    n_explicit = resolve(name_col)
    if c_explicit and n_explicit:
        return c_explicit, n_explicit

    def score_code_column(raw: object) -> float:
        ser = df[raw].dropna().head(80)
        if ser.empty:
            return 0.0
        ok = sum(1 for x in ser if _cell_to_code_str(x) is not None)
        return ok / len(ser)

    def score_name_column(raw: object) -> float:
        ser = df[raw].dropna().astype(str).str.strip()
        ser = ser[ser != ""]
        if ser.empty:
            return 0.0
        sample = ser.head(80)
        ok = sum(1 for x in sample if len(x) >= 2 and not x.isdigit())
        return ok / len(sample)

    best_c: str | None = c_explicit
    best_n: str | None = n_explicit
    indexed = [(i, raw) for i, raw in enumerate(raw_cols) if _normalize_header(raw)]

    if not best_c:
        cons_code = [
            (raw, score_code_column(raw), i)
            for i, raw in indexed
            if _is_constituent_stock_code_header(_normalize_header(raw))
        ]
        if cons_code:
            cons_code.sort(key=lambda t: (-t[1], t[2]))
            if cons_code[0][1] >= 0.5:
                best_c = cons_code[0][0]
        if not best_c:
            header_bonus: list[tuple[object, float, int]] = []
            for i, raw in indexed:
                norm = _normalize_header(raw)
                low = norm.lower()
                bonus = 0.0
                for h in _CODE_HEADER_HINTS:
                    if h.lower() in low:
                        bonus = 0.5
                        break
                if _is_index_meta_code_header(norm):
                    bonus -= 2.0
                header_bonus.append((raw, score_code_column(raw) + bonus, i))
            header_bonus.sort(key=lambda t: (-t[1], t[2]))
            if header_bonus and header_bonus[0][1] >= 0.5:
                best_c = header_bonus[0][0]

    if not best_n:
        cons_name = [
            (raw, score_name_column(raw), i)
            for i, raw in indexed
            if raw != best_c
            and _is_constituent_stock_name_header(_normalize_header(raw))
        ]
        if cons_name:
            cons_name.sort(key=lambda t: (-t[1], t[2]))
            if cons_name[0][1] >= 0.3:
                best_n = cons_name[0][0]
        if not best_n:
            candidates = [(i, raw) for i, raw in indexed if raw != best_c]
            header_bonus_n: list[tuple[object, float, int]] = []
            for i, raw in candidates:
                norm = _normalize_header(raw)
                low = norm.lower()
                bonus = 0.0
                for h in _NAME_HEADER_HINTS:
                    if h.lower() in low:
                        bonus = 0.5
                        break
                if "指数名称" in norm or "index name" in low:
                    bonus -= 2.0
                header_bonus_n.append((raw, score_name_column(raw) + bonus, i))
            header_bonus_n.sort(key=lambda t: (-t[1], t[2]))
            if header_bonus_n and header_bonus_n[0][1] >= 0.3:
                best_n = header_bonus_n[0][0]

    if not best_c or not best_n:
        print("无法自动识别代码列/名称列，请用 --code-col / --name-col 指定。", file=sys.stderr)
        print("表头:", list(df.columns), file=sys.stderr)
        print(df.head(5).to_string(), file=sys.stderr)
        raise SystemExit(2)

    return best_c, best_n


def _build_holdings(df: pd.DataFrame, code_col: str, name_col: str) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for _, row in df.iterrows():
        code = _cell_to_code_str(row.get(code_col))
        name_raw = row.get(name_col)
        if name_raw is None or (isinstance(name_raw, float) and pd.isna(name_raw)):
            continue
        name = str(name_raw).strip()
        if not code or not name:
            continue
        seen[code] = name
    return [{"code": c, "name": seen[c]} for c in sorted(seen.keys())]


def _load_holdings_json(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    rows = doc.get("holdings")
    if not isinstance(rows, list):
        raise ValueError("JSON 缺少 holdings 数组")
    out: list[dict[str, str]] = []
    for h in rows:
        if isinstance(h, dict) and isinstance(h.get("code"), str) and isinstance(h.get("name"), str):
            out.append({"code": h["code"], "name": h["name"]})
    return out


def _merge_holdings(
    existing: list[dict[str, str]], from_excel: list[dict[str, str]]
) -> tuple[list[dict[str, str]], int]:
    """同 code 保留 existing 中的名称与条目；仅追加 Excel 中的新 code。输出按 code 排序。"""
    merged: dict[str, str] = {}
    for h in existing:
        c = h.get("code", "").strip()
        n = h.get("name", "").strip()
        if c and n:
            merged[c] = n
    before = len(merged)
    for h in from_excel:
        c = h.get("code", "").strip()
        n = h.get("name", "").strip()
        if c and n and c not in merged:
            merged[c] = n
    added = len(merged) - before
    return [{"code": c, "name": merged[c]} for c in sorted(merged.keys())], added


def _write_watchlist_hs300_json(out_path: str, comment: str, holdings: list[dict[str, str]]) -> None:
    chunks: list[str] = [
        "{\n",
        '  "_comment": ' + json.dumps(comment, ensure_ascii=False) + ",\n",
        '  "holdings": [\n',
    ]
    last_i = len(holdings) - 1
    for i, h in enumerate(holdings):
        qc = json.dumps(h["code"], ensure_ascii=False)
        qn = json.dumps(h["name"], ensure_ascii=False)
        line = f'    {{ "code": {qc}, "name": {qn} }}'
        if i != last_i:
            line += ","
        line += "\n"
        chunks.append(line)
    chunks.append("  ]\n")
    chunks.append("}\n")
    text = "".join(chunks)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Excel 更新 watchlist_hs300.json")
    ap.add_argument("excel", help="Excel 文件路径（.xlsx）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    ap.add_argument("--sheet", default="0", help="工作表：名称或 0 起数字索引（默认 0）")
    ap.add_argument("--code-col", default=None, help="代码列表头（与 Excel 一致）")
    ap.add_argument("--name-col", default=None, help="名称列表头")
    ap.add_argument(
        "--merge-existing",
        action="store_true",
        help="与 --out 已有 JSON 合并：仅追加新 code，同代码保留原名称",
    )
    ap.add_argument(
        "--strict-300",
        action="store_true",
        help="非 300 条则返回码 1（与 --merge-existing 并用时一般不适用）",
    )
    args = ap.parse_args()

    excel_path = os.path.abspath(args.excel)
    if not os.path.isfile(excel_path):
        print(f"文件不存在: {excel_path}", file=sys.stderr)
        return 2

    sheet: str | int = args.sheet
    if sheet.isdigit():
        sheet = int(sheet)

    try:
        df = pd.read_excel(excel_path, sheet_name=sheet, dtype=object)
    except ImportError as e:
        print(str(e), file=sys.stderr)
        print("请安装: pip install openpyxl", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"读取 Excel 失败: {e}", file=sys.stderr)
        return 2

    if df.empty:
        print("Excel 工作表为空", file=sys.stderr)
        return 2

    code_col, name_col = _pick_columns(df, args.code_col, args.name_col)
    holdings = _build_holdings(df, code_col, name_col)
    n_excel = len(holdings)
    if n_excel == 0:
        print("未解析到任何有效 code+name 行", file=sys.stderr)
        return 2

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    added = 0
    if args.merge_existing:
        if not os.path.isfile(out_path):
            print(f"--merge-existing 需要已有文件: {out_path}", file=sys.stderr)
            return 2
        try:
            existing = _load_holdings_json(out_path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"读取已有 JSON 失败: {e}", file=sys.stderr)
            return 2
        holdings, added = _merge_holdings(existing, holdings)
        comment = (
            "成份池（仅 code、name）：在既有 watchlist 上按代码去重合并，并追加 "
            f"{os.path.basename(excel_path)} 中的新标的（同代码保留原名称）。勿手改 holdings；"
            "追加其它指数表请运行：python3 backend/scripts/update_watchlist_hs300_from_excel.py "
            "<xls|xlsx> --merge-existing"
        )
    else:
        comment = (
            "沪深300成份股名单（仅 code、name）。数据来源：本地 Excel "
            f"{os.path.basename(excel_path)} 。勿手改 holdings；刷新请运行："
            "python3 backend/scripts/update_watchlist_hs300_from_excel.py <xls|xlsx>"
        )

    n = len(holdings)
    _write_watchlist_hs300_json(out_path, comment, holdings)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    print(
        f"已写入 {out_path} ，共 {n} 条（代码列: {code_col!r}，名称列: {name_col!r}，UTC {ts}）"
    )
    if args.merge_existing:
        print(f"合并：Excel 解析 {n_excel} 条，新增 {added} 个代码（与已有去重后合计 {n}）")
    if not args.merge_existing and n != 300:
        print(f"提示: 成份数量为 {n}（沪深300 通常为 300）", file=sys.stderr)
    if args.strict_300 and n != 300:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
