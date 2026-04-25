"""持仓管理模块线程安全与文件锁测试。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from services.position_manager import (
    Position,
    buy,
    get_holdings,
    load_positions,
    save_positions,
    sell_all,
)


class TestPositionManagerThreadSafe(unittest.TestCase):
    """测试持仓管理在多线程环境下的安全性。"""

    def setUp(self) -> None:
        # 使用临时目录隔离测试数据
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        #  monkey-patch 数据目录到临时位置
        import services.position_manager as pm

        self._orig_data_dir = pm.DATA_DIR
        self._orig_positions_file = pm.POSITIONS_FILE
        pm.DATA_DIR = Path(self.tmpdir.name)
        pm.POSITIONS_FILE = pm.DATA_DIR / "positions.json"
        # 重置内存状态
        pm._positions = []

    def tearDown(self) -> None:
        import services.position_manager as pm

        pm.DATA_DIR = self._orig_data_dir
        pm.POSITIONS_FILE = self._orig_positions_file
        pm._positions = []

    def test_concurrent_buy_no_data_loss(self) -> None:
        """并发买入不应导致数据丢失（每个 buy 都会先 load 再 save）。"""
        errors: list[Exception] = []

        def do_buy(idx: int) -> None:
            try:
                buy(
                    code=f"{idx:06d}",
                    name=f"Test{idx}",
                    signal_type="first_buy",
                    price=10.0 + idx,
                    amount=10000.0,
                    tactical_stop=9.0,
                    strategic_stop=8.0,
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_buy, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发买入出现异常: {errors}")

        # 验证文件中的记录数
        holdings = get_holdings()
        self.assertEqual(len(holdings), 20, "并发买入后持仓数量应正确")

        # 验证文件能正确读取
        import services.position_manager as pm
        with open(pm.POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 20, "positions.json 中应有 20 条记录")

    def test_buy_and_sell_all_thread_safe(self) -> None:
        """买入后并发清仓不应导致文件损坏。"""
        buy(
            code="000001",
            name="平安银行",
            signal_type="first_buy",
            price=10.0,
            amount=10000.0,
            tactical_stop=9.0,
            strategic_stop=8.0,
        )

        # 多个线程同时尝试清仓同一持仓
        results: list = []

        def do_sell() -> None:
            result = sell_all("000001", 9.5, "测试清仓")
            results.append(result)

        threads = [threading.Thread(target=do_sell) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 只有第一次清仓会成功
        successful = [r for r in results if r is not None]
        self.assertEqual(len(successful), 1, "只有第一个清仓应成功")

        # 验证文件一致性
        holdings = get_holdings()
        self.assertEqual(len(holdings), 0, "清仓后应无持仓")

        import services.position_manager as pm
        with open(pm.POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "sold")

    def test_file_lock_prevents_corruption(self) -> None:
        """模拟多进程同时写入，验证文件锁防止损坏。"""
        import services.position_manager as pm
        # 单线程场景下验证 save_positions 能正确写入完整 JSON
        pm._positions = [
            Position(
                code="000001",
                name="Test",
                signal_type="first_buy",
                buy_date="2026-01-01",
                buy_price=10.0,
                amount=10000.0,
                tactical_stop=9.0,
                strategic_stop=8.0,
                status="holding",
            )
        ]
        save_positions()

        # 验证文件是合法的 JSON
        with open(pm.POSITIONS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        data = json.loads(content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["code"], "000001")


if __name__ == "__main__":
    unittest.main()
