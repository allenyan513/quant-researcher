"""Buy-and-hold — the comparison benchmark (ported from quant-engine).

Buy the full size on the first bar, then hold forever.
"""

from __future__ import annotations

from quant_researcher.engine.strategy.base import BaseStrategy


class BuyAndHold(BaseStrategy):
    def __init__(self, symbol: str, size: int = 100) -> None:
        super().__init__()
        self.symbol = symbol
        self.size = int(size)
        self._bought = False

    def on_bar(self) -> None:
        if not self._bought:
            self.buy(self.symbol, self.size)
            self._bought = True
