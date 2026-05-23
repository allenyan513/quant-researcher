"""Donchian-channel breakout — turtle-style (ported from quant-engine).

Close above the `entry_period`-day high → buy; close below the
`exit_period`-day low → flatten. Channels exclude the current bar (`[:-1]`).
"""

from __future__ import annotations

from quant_researcher.engine.indicators import donchian
from quant_researcher.engine.strategy.base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        entry_period: int = 20,
        exit_period: int = 10,
        size: int = 100,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.entry_period = int(entry_period)
        self.exit_period = int(exit_period)
        self.size = int(size)

    def on_bar(self) -> None:
        needed = self.entry_period + 1
        if not self.bar_data.has_enough_bars(self.symbol, needed):
            return

        bar = self.bar_data.current(self.symbol)
        pos = self.get_position(self.symbol)

        highs = self.bar_data.history(self.symbol, "high", needed)
        lows = self.bar_data.history(self.symbol, "low", needed)

        # 入场通道: 用 entry_period 算（不含当前 bar → 用 [:-1]）
        entry_upper = donchian(highs[:-1], lows[:-1], self.entry_period).upper[-1]

        # 离场通道: 用 exit_period 算
        if len(highs) > self.exit_period:
            exit_lower = donchian(highs[:-1], lows[:-1], self.exit_period).lower[-1]
        else:
            exit_lower = None

        # 突破上轨 → 买入
        if pos == 0 and bar.close > entry_upper:
            self.buy(self.symbol, self.size)
        # 跌破下轨 → 卖出
        elif pos > 0 and exit_lower is not None and bar.close < exit_lower:
            self.sell(self.symbol, pos)
