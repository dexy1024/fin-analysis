"""K 线响应缓存 LRU 淘汰策略测试。"""
from __future__ import annotations

import time
import unittest

from services.indicators import (
    _kline_cache_delete_all_for_symbol_period,
    _kline_cache_get,
    _kline_cache_set,
    _KLINE_RESP_CACHE,
    _KLINE_RESP_CACHE_MAX_ITEMS,
)


class TestKlineCacheLRU(unittest.TestCase):
    """测试 K 线缓存的 LRU 淘汰机制。"""

    def setUp(self) -> None:
        # 清空全局缓存，避免测试间互相影响
        _KLINE_RESP_CACHE.clear()

    def tearDown(self) -> None:
        _KLINE_RESP_CACHE.clear()

    def test_lru_eviction_oldest_unused(self) -> None:
        """当缓存超过上限时，最早写入且未被访问的条目应被淘汰。"""
        max_items = _KLINE_RESP_CACHE_MAX_ITEMS

        # 填充缓存到上限
        for i in range(max_items):
            key = (f"sym_{i:03d}", "daily", "2025-01-01", "2025-12-31")
            _kline_cache_set(key, {"data": [i]}, symbol=f"sym_{i:03d}", period="daily")

        self.assertEqual(len(_KLINE_RESP_CACHE), max_items, "缓存应达到上限")

        # 访问第 0 个条目（更新其 LRU 时间戳）
        first_key = ("sym_000", "daily", "2025-01-01", "2025-12-31")
        _kline_cache_get(first_key)

        # 写入一个新条目，触发淘汰
        new_key = ("sym_new", "daily", "2025-01-01", "2025-12-31")
        _kline_cache_set(new_key, {"data": ["new"]}, symbol="sym_new", period="daily")

        self.assertEqual(len(_KLINE_RESP_CACHE), max_items, "淘汰后缓存应仍保持上限")
        # 最近被访问的 first_key 不应被淘汰
        self.assertIsNotNone(
            _kline_cache_get(first_key),
            "最近访问的条目不应被 LRU 淘汰",
        )
        # 新写入的条目应在缓存中
        self.assertIsNotNone(
            _kline_cache_get(new_key),
            "新写入的条目应在缓存中",
        )

    def test_ttl_expires_old_entries(self) -> None:
        """超过 TTL 的条目应被自动清理。"""
        key = ("sym_ttl", "60", "2025-01-01", "2025-12-31")
        _kline_cache_set(key, {"data": [1]}, symbol="sym_ttl", period="60")

        # 刚写入应能命中
        self.assertIsNotNone(_kline_cache_get(key))

        # 由于无法真的等待 300 秒，我们直接修改缓存中的时间戳为过期
        ts, src_mtime, data = _KLINE_RESP_CACHE[key]
        # 将时间戳设为 400 秒前
        _KLINE_RESP_CACHE[key] = (ts - 400, src_mtime, data)

        # 再次获取应返回 None（已过期）
        self.assertIsNone(_kline_cache_get(key), "超过 TTL 的条目应被清除")

    def test_delete_all_for_symbol_period(self) -> None:
        """按 symbol+period 清除缓存应只影响对应条目。"""
        key_daily = ("sym_001", "daily", "2025-01-01", "2025-12-31")
        key_60m = ("sym_001", "60", "2025-01-01", "2025-12-31")
        key_other = ("sym_002", "daily", "2025-01-01", "2025-12-31")

        _kline_cache_set(key_daily, {"data": [1]}, symbol="sym_001", period="daily")
        _kline_cache_set(key_60m, {"data": [2]}, symbol="sym_001", period="60")
        _kline_cache_set(key_other, {"data": [3]}, symbol="sym_002", period="daily")

        _kline_cache_delete_all_for_symbol_period("sym_001", "daily")

        self.assertIsNone(_kline_cache_get(key_daily), "daily 缓存应被清除")
        self.assertIsNotNone(_kline_cache_get(key_60m), "60m 缓存不应被清除")
        self.assertIsNotNone(_kline_cache_get(key_other), "其他 symbol 缓存不应被清除")


if __name__ == "__main__":
    unittest.main()
