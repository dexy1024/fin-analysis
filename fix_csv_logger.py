#!/usr/bin/env python3
"""
脚本用于修改 csv_logger.py 添加 60m交易 字段
"""

import re

# 读取文件
file_path = "/Users/yuguoq/Desktop/CursorProject/fin-analysis/backend/utils/csv_logger.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改 CSV_HEADERS - 在 "实际交易动作" 后添加 "60m交易"
old_headers = '''CSV_HEADERS = [
    "时间",
    "实际交易动作",
    "是否持仓",'''

new_headers = '''CSV_HEADERS = [
    "时间",
    "实际交易动作",
    "60m交易",
    "是否持仓",'''

content = content.replace(old_headers, new_headers)

# 2. 在 _fmt_float4 函数后添加 _get_60m_trade_action 函数
old_func = '''def _fmt_float4(value: Any) -> str:
    """将值格式化为四位小数字符串（用于 DIF/DEA）；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""


def _h15_signal_detail'''

new_func = '''def _fmt_float4(value: Any) -> str:
    """将值格式化为四位小数字符串（用于 DIF/DEA）；None 或无效值返回空字符串。"""
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value) if value != "" else ""


def _get_60m_trade_action(chan_sig: str, pen_direction: str) -> str:
    """
    计算「60m交易」字段。

    逻辑：
    - 缠论信号含'买' + 60m笔方向 == '向下' → 买入
    - 缠论信号含'卖' + 60m笔方向 == '向上' → 卖出
    - 其他情况 → 观望
    """
    has_buy = "买" in chan_sig
    has_sell = "卖" in chan_sig

    # 买入：信号含买 + 笔向下
    if has_buy and pen_direction == "向下":
        return "买入"

    # 卖出：信号含卖 + 笔向上
    if has_sell and pen_direction == "向上":
        return "卖出"

    # 其他情况
    return "观望"


def _h15_signal_detail'''

content = content.replace(old_func, new_func)

# 3. 修改 build_snapshot_data 的返回字典 - 在 "实际交易动作" 后添加 "60m交易"
old_return = '''    return {
        "时间": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "实际交易动作": trade_sig,
        "是否持仓": is_holding,'''

new_return = '''    return {
        "时间": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "实际交易动作": trade_sig,
        "60m交易": _get_60m_trade_action(chan_sig, pen_dir),
        "是否持仓": is_holding,'''

content = content.replace(old_return, new_return)

# 写入文件
with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("修改完成！")
print("已添加：")
print("1. CSV_HEADERS 中的 '60m交易' 字段")
print("2. _get_60m_trade_action 函数")
print("3. build_snapshot_data 返回字典中的 '60m交易' 字段")
