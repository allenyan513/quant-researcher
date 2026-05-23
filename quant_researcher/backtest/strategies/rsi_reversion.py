"""RSI mean reversion (ported from quant-engine).

RSI below `oversold` → buy; above `overbought` → flatten.
"""

from __future__ import annotations

from quant_researcher.engine.indicators import rsi
from quant_researcher.engine.strategy.base import BaseStrategy


class RSIReversion(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        size: int = 100,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.period = int(period)
        self.oversold = float(oversold)
        self.overbought = float(overbought)
        self.size = int(size)

    def on_bar(self) -> None:
        needed = self.period + 1
        if not self.bar_data.has_enough_bars(self.symbol, needed):
            return

        closes = self.bar_data.history(self.symbol, "close", needed)
        current_rsi = rsi(closes, self.period)[-1]

        pos = self.get_position(self.symbol)

        # 超卖 → 买入
        if current_rsi < self.oversold and pos == 0:
            self.buy(self.symbol, self.size)
        # 超买 → 卖出
        elif current_rsi > self.overbought and pos > 0:
            self.sell(self.symbol, pos)
