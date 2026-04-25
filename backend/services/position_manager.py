"""
持仓管理与止损监控模块

功能：
- 记录一买/二买买入持仓（代码、买入价、金额、止损线）
- 定时检查持仓止损（战术止损：跌破底分型低点；战略止损：跌破一买绝对低点）
- 触发清仓时写入交易日志并 SSE 推送告警

数据持久化：backend/data/positions.json
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
POSITIONS_FILE = DATA_DIR / "positions.json"

# SSE 广播回调（由 main.py 设置）
_sse_callback: Optional[Callable[[str, str, float], None]] = None


def set_sse_callback(callback: Callable[[str, str, float], None]) -> None:
    """设置 SSE 广播回调函数"""
    global _sse_callback
    _sse_callback = callback


@dataclass
class Position:
    code: str
    name: str
    signal_type: str  # "first_buy" | "second_buy"
    buy_date: str
    buy_price: float
    amount: float  # 买入金额（元）
    tactical_stop: float  # 战术止损线（底分型低点）
    strategic_stop: float  # 战略止损线（一买绝对低点）
    status: str  # "holding" | "sold"
    sell_date: Optional[str] = None
    sell_price: Optional[float] = None
    sell_reason: Optional[str] = None


_positions: List[Position] = []


def load_positions() -> List[Position]:
    """从 JSON 加载持仓记录"""
    global _positions
    if not POSITIONS_FILE.exists():
        _positions = []
        return _positions
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            _positions = [Position(**item) for item in data]
    except Exception as e:
        logging.warning("[position_manager] 加载持仓失败: %s", e)
        _positions = []
    return _positions


def save_positions() -> None:
    """保存持仓记录到 JSON"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in _positions], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("[position_manager] 保存持仓失败: %s", e)


def buy(
    code: str,
    name: str,
    signal_type: str,
    price: float,
    amount: float,
    tactical_stop: float,
    strategic_stop: float,
) -> Position:
    """
    记录买入持仓

    Args:
        code: 股票代码
        name: 股票名称
        signal_type: "first_buy" 或 "second_buy"
        price: 买入价格
        amount: 买入金额（元）
        tactical_stop: 战术止损线（底分型最低点）
        strategic_stop: 战略止损线（一买绝对低点）
    """
    load_positions()
    position = Position(
        code=code,
        name=name,
        signal_type=signal_type,
        buy_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        buy_price=price,
        amount=amount,
        tactical_stop=tactical_stop,
        strategic_stop=strategic_stop,
        status="holding",
    )
    _positions.append(position)
    save_positions()
    logging.info(
        "[position_manager] 买入: %s %s 金额=%.0f元 @ %.2f, 战术止损=%.2f, 战略止损=%.2f",
        code, name, amount, price, tactical_stop, strategic_stop
    )
    return position


def sell_all(code: str, current_price: float, reason: str) -> Optional[Position]:
    """
    清仓指定代码的持仓

    Returns:
        被清仓的 Position，如果没有持仓则返回 None
    """
    load_positions()
    for p in _positions:
        if p.code == code and p.status == "holding":
            p.status = "sold"
            p.sell_date = datetime.now().strftime("%Y-%m-%d %H:%M")
            p.sell_price = current_price
            p.sell_reason = reason
            save_positions()
            logging.info(
                "[position_manager] 清仓: %s @ %.2f, 原因: %s, 亏损: %.2f元",
                code, current_price, reason,
                (p.buy_price - current_price) * (p.amount / p.buy_price)
            )
            # SSE 推送止损告警
            if _sse_callback:
                try:
                    _sse_callback(code, reason, current_price)
                except Exception as e:
                    logging.warning("[position_manager] SSE 推送失败: %s", e)
            return p
    return None


def get_holdings() -> List[Position]:
    """获取当前所有持仓"""
    load_positions()
    return [p for p in _positions if p.status == "holding"]


def get_all_positions() -> List[Position]:
    """获取所有持仓记录（含已清仓）"""
    load_positions()
    return list(_positions)


def check_stop_loss(code: str, current_price: float) -> Optional[Dict]:
    """
    检查指定代码的持仓是否触发止损

    Returns:
        {"triggered": True, "reason": str, "position": Position} 或 None
    """
    load_positions()
    for p in _positions:
        if p.code == code and p.status == "holding":
            # 战术止损：跌破底分型低点
            if current_price < p.tactical_stop:
                return {
                    "triggered": True,
                    "reason": f"跌破战术止损线({p.tactical_stop:.2f})",
                    "position": p,
                }
            # 战略止损：跌破一买绝对低点
            if current_price < p.strategic_stop:
                return {
                    "triggered": True,
                    "reason": f"跌破战略止损线({p.strategic_stop:.2f})",
                    "position": p,
                }
    return None


def check_all_stop_loss(prices: Dict[str, float]) -> List[Dict]:
    """
    批量检查所有持仓的止损

    Args:
        prices: {code: current_price} 字典

    Returns:
        触发止损的列表，每项包含 code、reason、position
    """
    results: List[Dict] = []
    for code, price in prices.items():
        result = check_stop_loss(code, price)
        if result and result["triggered"]:
            sell_all(code, price, result["reason"])
            results.append({
                "code": code,
                "reason": result["reason"],
                "price": price,
                "position": asdict(result["position"]),
            })
    return results
