"""
趋势类指标 — SMA / EMA / MACD
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def sma(data: np.ndarray, period: int) -> np.ndarray:
    """
    简单移动平均。

    返回与 data 等长的数组，前 period-1 个值为 NaN。
    """
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    out = np.full(len(data), np.nan, dtype=float)
    # 用 cumsum 技巧实现 O(n) 计算
    cumsum = np.cumsum(data, dtype=float)
    out[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return out


def ema(data: np.ndarray, period: int) -> np.ndarray:
    """
    指数移动平均。

    以前 period 个值的 SMA 作为起始值，之后递推。
    """
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    out = np.full(len(data), np.nan, dtype=float)
    k = 2.0 / (period + 1)
    # 起始值 = 前 period 个的 SMA
    out[period - 1] = data[:period].mean()
    for i in range(period, len(data)):
        out[i] = data[i] * k + out[i - 1] * (1 - k)
    return out


@dataclass
class MACDResult:
    """MACD 计算结果。"""
    macd_line: np.ndarray      # MACD 线 = fast_ema - slow_ema
    signal_line: np.ndarray    # 信号线 = MACD 线的 EMA
    histogram: np.ndarray      # 柱状图 = MACD - Signal


def macd(
    data: np.ndarray,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """
    MACD — Moving Average Convergence Divergence。

    经典参数: (12, 26, 9)
    - MACD 线 > 0: 短期趋势强于长期 → 偏多
    - 信号线交叉: MACD 上穿信号线 → 买入信号
    - 柱状图: 正转负 / 负转正 → 动量变化
    """
    fast_ema = ema(data, fast_period)
    slow_ema = ema(data, slow_period)
    macd_line = fast_ema - slow_ema

    # 信号线 = MACD 线的 EMA（只在 MACD 有值的部分计算）
    valid_start = slow_period - 1  # MACD 从这里开始有值
    signal_line = np.full_like(macd_line, np.nan)
    if len(data) > valid_start + signal_period:
        macd_valid = macd_line[valid_start:]
        signal_valid = ema(macd_valid, signal_period)
        signal_line[valid_start:] = signal_valid

    histogram = macd_line - signal_line

    return MACDResult(
        macd_line=macd_line,
        signal_line=signal_line,
        histogram=histogram,
    )
