"""
数据源 / 调度任务中可预期的业务异常集合。

捕获后记录 warning 并降级继续；未预期异常记录完整堆栈并重新抛出，
避免 KeyboardInterrupt、SystemExit 等被静默吞掉。
"""

from __future__ import annotations

import json

import pandas as pd
import requests.exceptions

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:

    class YFRateLimitError(Exception):
        """旧版 yfinance 无此类型时的占位"""

EXPECTED_BUSINESS_EXCEPTIONS = (
    ValueError,
    OSError,
    TypeError,
    KeyError,
    RuntimeError,
    json.JSONDecodeError,
    requests.exceptions.RequestException,
    pd.errors.EmptyDataError,
    YFRateLimitError,
)

# 向后兼容 kline_scheduler 旧名
SCHEDULER_EXPECTED_EXCEPTIONS = EXPECTED_BUSINESS_EXCEPTIONS
