"""
突破类指标 — Donchian Channel
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class DonchianResult:
    """Donchian Channel 计算结果。"""
    upper: np.ndarray    # 过去 N 日最高价
    lower: np.ndarray    # 过去 N 日最低价
    middle: np.ndarray   # (upper + lower) / 2


def donchian(
    high: np.ndarray,
    low: np.ndarray,
    period: int = 20,
) -> DonchianResult:
    """
    Donchian Channel — 唐奇安通道（海龟交易法核心）。

    - 价格突破上轨 → 创 N 日新高 → 买入信号
    - 价格跌破下轨 → 创 N 日新低 → 卖出信号

    经典用法（海龟交易法）:
    - 入场: 突破 20 日通道
    - 离场: 跌破 10 日通道
    """
    n = len(high)
    upper = np.full(n, np.nan, dtype=float)
    lower = np.full(n, np.nan, dtype=float)

    for i in range(period - 1, n):
        upper[i] = high[i - period + 1: i + 1].max()
        lower[i] = low[i - period + 1: i + 1].min()

    middle = (upper + lower) / 2

    return DonchianResult(upper=upper, lower=lower, middle=middle)
