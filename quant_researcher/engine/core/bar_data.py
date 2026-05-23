"""
Bar 数据结构 — OHLCV + 多标的数据容器。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class Bar:
    """单根 K 线。"""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __repr__(self) -> str:
        return (
            f"Bar({self.symbol} {self.timestamp:%Y-%m-%d} "
            f"O={self.open:.2f} H={self.high:.2f} "
            f"L={self.low:.2f} C={self.close:.2f} V={self.volume})"
        )


class BarData:
    """
    策略可访问的数据视图。

    提供当前 bar 和历史数据查询，防止 look-ahead bias —
    只暴露 <= current_index 的数据。
    """

    def __init__(self) -> None:
        # symbol -> list of Bars (按时间升序)
        self._bars: dict[str, list[Bar]] = {}
        # symbol -> 当前已推进到的索引
        self._current_idx: dict[str, int] = {}

    @property
    def symbols(self) -> list[str]:
        return list(self._bars.keys())

    def add_symbol_bars(self, symbol: str, bars: list[Bar]) -> None:
        """加载某标的全部历史 bar（引擎初始化时调用）。"""
        self._bars[symbol] = bars
        self._current_idx[symbol] = -1  # 尚未开始

    def advance(self, symbol: str) -> Bar | None:
        """推进一根 bar，返回最新 bar；到末尾返回 None。"""
        idx = self._current_idx.get(symbol, -1) + 1
        bars = self._bars.get(symbol, [])
        if idx >= len(bars):
            return None
        self._current_idx[symbol] = idx
        return bars[idx]

    def current(self, symbol: str) -> Bar | None:
        """当前 bar。"""
        idx = self._current_idx.get(symbol, -1)
        if idx < 0:
            return None
        return self._bars[symbol][idx]

    def history(self, symbol: str, field: str, length: int) -> np.ndarray:
        """
        获取最近 N 根 bar 的某个字段值（不含未来数据）。

        用法: bar_data.history("AAPL", "close", 20)
        返回: np.array，最旧在前，最新在后
        """
        idx = self._current_idx.get(symbol, -1)
        if idx < 0:
            return np.array([])
        bars = self._bars[symbol]
        start = max(0, idx - length + 1)
        return np.array([getattr(bars[i], field) for i in range(start, idx + 1)])

    def has_enough_bars(self, symbol: str, length: int) -> bool:
        """当前是否有足够的历史数据。"""
        idx = self._current_idx.get(symbol, -1)
        return idx + 1 >= length
