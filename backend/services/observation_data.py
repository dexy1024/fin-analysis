"""观察标的 JSON：A 股/ETF（observation.json）、港股（observation_hk.json）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OBSERVATION_FILE = DATA_DIR / "observation.json"
OBSERVATION_HK_FILE = DATA_DIR / "observation_hk.json"
def _resolve_hide_when_broken(raw: Any, default: bool) -> bool:
    """破位（60m 现价 < MIN(日线 A-ZD, C-ZD)）时是否隐藏 Tab；默认 True。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("false", "0", "no", "off"):
            return False
        if s in ("true", "1", "yes", "on"):
            return True
    return default


def _read_observations_file(path: Path, *, log_label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        default_hide = _resolve_hide_when_broken(data.get("hideWhenBroken"), True)
        items = data.get("observations", [])
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("code"):
                out.append(
                    {
                        "code": str(item["code"]).strip(),
                        "name": str(item.get("name", "")).strip(),
                        "hideWhenBroken": _resolve_hide_when_broken(
                            item.get("hideWhenBroken"), default_hide
                        ),
                    }
                )
        return out
    except (OSError, json.JSONDecodeError, TypeError):
        logging.warning("%s: 读取 %s 失败", log_label, path.name)
        return []


def load_observation_items(*, include_hk: bool = True) -> list[dict[str, Any]]:
    """合并 observation.json 与（可选）observation_hk.json，去重保留先出现的项。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _read_observations_file(OBSERVATION_FILE, log_label="observation_data"):
        code = item["code"]
        if code not in seen:
            merged.append(item)
            seen.add(code)
    if include_hk:
        for item in _read_observations_file(OBSERVATION_HK_FILE, log_label="observation_data"):
            code = item["code"]
            if code not in seen:
                merged.append(item)
                seen.add(code)
    return merged


def load_observation_hk_items() -> list[dict[str, Any]]:
    return _read_observations_file(OBSERVATION_HK_FILE, log_label="observation_data")


def load_observation_items_for_frontend(*, include_hk: bool = True) -> list[dict[str, Any]]:
    """前端展示用：observation.json + observation_hk.json，去重。"""
    return load_observation_items(include_hk=include_hk)


def load_observation_pairs(*, include_hk: bool = True) -> list[tuple[str, str]]:
    return [(item["code"], item["name"]) for item in load_observation_items(include_hk=include_hk)]


WATCHLIST_FILE = DATA_DIR / "watchlist.json"


def load_watchlist_observation_symbols(*, include_hk: bool = True) -> list[tuple[str, str]]:
    """读取 watchlist.json + observation（+ 可选 observation_hk），去重后的 (code, name)。"""
    symbols: list[tuple[str, str]] = []
    if WATCHLIST_FILE.is_file():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            for item in data.get("holdings", []):
                if isinstance(item, dict) and item.get("code"):
                    symbols.append((str(item["code"]).strip(), str(item.get("name", "")).strip()))
        except (OSError, json.JSONDecodeError, TypeError):
            logging.warning("observation_data: 读取 watchlist.json 失败")
    for code, name in load_observation_pairs(include_hk=include_hk):
        if not any(c == code for c, _ in symbols):
            symbols.append((code, name))
    return symbols
