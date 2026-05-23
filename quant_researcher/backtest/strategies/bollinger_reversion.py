"""Bollinger-band mean reversion (ported from quant-engine).

Price below lower band → buy (oversold bounce); exit at the middle band
(default) or above the upper band.
"""

from __future__ import annotations

from quant_researcher.engine.indicators import bollinger
from quant_researcher.engine.strategy.base import BaseStrategy


class BollingerReversion(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        period: int = 20,
        num_std: float = 2.0,
        exit_at_middle: bool = True,
        size: int = 100,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.period = int(period)
        self.num_std = float(num_std)
        self.exit_at_middle = bool(exit_at_middle)
        self.size = int(size)

    def on_bar(self) -> None:
        if not self.bar_data.has_enough_bars(self.symbol, self.period):
            return

        closes = self.bar_data.history(self.symbol, "close", self.period)
        bands = bollinger(closes, self.period, self.num_std)

        price = closes[-1]
        upper = bands.upper[-1]
        middle = bands.middle[-1]
        lower = bands.lower[-1]

        pos = self.get_position(self.symbol)

        # 跌破下轨 → 买入
        if price < lower and pos == 0:
            self.buy(self.symbol, self.size)
        elif pos > 0:
            if self.exit_at_middle:
                if price >= middle:  # 回归中轨 → 平仓
                    self.sell(self.symbol, pos)
            elif price > upper:  # 突破上轨 → 平仓
                self.sell(self.symbol, pos)
