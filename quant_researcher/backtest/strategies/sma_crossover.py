"""SMA crossover — the canonical built-in strategy (ported from quant-engine).

Golden cross (fast SMA crosses above slow) → buy; death cross → flatten. Used
as the e2e smoke target for `qr backtest run`.
"""

from __future__ import annotations

from quant_researcher.engine.indicators import sma
from quant_researcher.engine.strategy.base import BaseStrategy


class SMACrossover(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        fast_period: int = 10,
        slow_period: int = 30,
        size: int = 100,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.fast_period = int(fast_period)
        self.slow_period = int(slow_period)
        self.size = int(size)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def on_bar(self) -> None:
        if not self.bar_data.has_enough_bars(self.symbol, self.slow_period):
            return

        closes = self.bar_data.history(self.symbol, "close", self.slow_period)
        fast = sma(closes, self.fast_period)[-1]
        slow = sma(closes, self.slow_period)[-1]

        pos = self.get_position(self.symbol)

        if self._prev_fast is not None and self._prev_slow is not None:
            # 金叉: 短期从下方穿越长期
            if self._prev_fast <= self._prev_slow and fast > slow:
                if pos == 0:
                    self.buy(self.symbol, self.size)
            # 死叉: 短期从上方穿越长期
            elif self._prev_fast >= self._prev_slow and fast < slow:
                if pos > 0:
                    self.sell(self.symbol, pos)

        self._prev_fast = fast
        self._prev_slow = slow
