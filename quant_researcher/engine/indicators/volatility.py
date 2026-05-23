"""
波动率类指标 — ATR / Bollinger Bands
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .trend import sma


def atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """
    ATR — Average True Range。

    True Range = max(
        当日最高 - 当日最低,
        |当日最高 - 昨日收盘|,
        |当日最低 - 昨日收盘|,
    )

    用途:
    - 止损位: 入场价 - 2*ATR
    - 仓位大小: 风险金额 / ATR → 得到应买股数
    - 波动率过滤: ATR 高 → 市场活跃

    返回与输入等长的数组，前 period 个值为 NaN。
    """
    n = len(close)
    if n < 2:
        return np.full(n, np.nan, dtype=float)

    tr = np.full(n, np.nan, dtype=float)

    # 第一根 bar: TR = high - low
    tr[0] = high[0] - low[0]

    # 后续: TR = max(H-L, |H-prevC|, |L-prevC|)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # ATR = TR 的 Wilder 平滑（等价于 EMA with alpha=1/period）
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return out

    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period

    return out


@dataclass
class BollingerResult:
    """Bollinger Bands 计算结果。"""
    upper: np.ndarray    # 上轨 = middle + num_std * std
    middle: np.ndarray   # 中轨 = SMA
    lower: np.ndarray    # 下轨 = middle - num_std * std


def bollinger(
    data: np.ndarray,
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult:
    """
    Bollinger Bands — 布林带。

    - 价格触碰上轨 → 可能超买 / 强势突破
    - 价格触碰下轨 → 可能超卖 / 弱势破位
    - 带宽收窄 → 即将出现大幅波动（squeeze）
    - 均值回归策略: 碰下轨买入，碰上轨卖出
    """
    middle = sma(data, period)

    # 滚动标准差
    std = np.full_like(data, np.nan, dtype=float)
    for i in range(period - 1, len(data)):
        std[i] = data[i - period + 1: i + 1].std(ddof=0)

    upper = middle + num_std * std
    lower = middle - num_std * std

    return BollingerResult(upper=upper, middle=middle, lower=lower)
