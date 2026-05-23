"""
动量类指标 — RSI
"""

from __future__ import annotations

import numpy as np


def rsi(data: np.ndarray, period: int = 14) -> np.ndarray:
    """
    RSI — Relative Strength Index。

    使用 Wilder 平滑法（指数移动平均）。

    - RSI > 70: 超买 → 可能回调
    - RSI < 30: 超卖 → 可能反弹
    - RSI = 50: 多空平衡

    返回与 data 等长的数组，前 period 个值为 NaN。
    """
    if len(data) < period + 1:
        return np.full_like(data, np.nan, dtype=float)

    out = np.full(len(data), np.nan, dtype=float)
    deltas = np.diff(data)

    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # 第一个值: 前 period 个变化的简单平均
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)

    # Wilder 平滑递推
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)

    return out
