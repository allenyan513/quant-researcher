"""
测试辅助工具 — 构造假数据，避免依赖网络。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.data.data_feed import DataFeed


def make_bars(
    symbol: str = "TEST",
    prices: list[float] | None = None,
    start_date: str = "2024-01-01",
    n: int = 10,
) -> list[Bar]:
    """
    生成假 Bar 数据。

    如果传 prices，则用 prices 作为 close（open/high/low 自动围绕 close 构造）。
    否则生成 n 根从 100.0 开始的 bar。
    """
    if prices is None:
        prices = [100.0 + i for i in range(n)]

    dt = datetime.strptime(start_date, "%Y-%m-%d")
    bars: list[Bar] = []
    for i, close in enumerate(prices):
        bars.append(Bar(
            symbol=symbol,
            timestamp=dt + timedelta(days=i),
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1_000_000,
        ))
    return bars


def make_bar_data(symbol: str = "TEST", prices: list[float] | None = None, n: int = 10) -> BarData:
    """创建已加载数据的 BarData（未推进）。"""
    bd = BarData()
    bd.add_symbol_bars(symbol, make_bars(symbol, prices, n=n))
    return bd


def advance_all(bar_data: BarData, steps: int | None = None) -> None:
    """推进 BarData 所有标的 steps 步（默认推到底）。"""
    for symbol in bar_data.symbols:
        count = steps or len(bar_data._bars[symbol])
        for _ in range(count):
            bar_data.advance(symbol)


class MockDataFeed(DataFeed):
    """用于集成测试的模拟数据源。"""

    def __init__(self, data: dict[str, list[Bar]]) -> None:
        self._data = data

    def fetch(self, symbol: str, start: str, end: str) -> list[Bar]:
        if symbol not in self._data:
            raise ValueError(f"No mock data for {symbol}")
        return self._data[symbol]
