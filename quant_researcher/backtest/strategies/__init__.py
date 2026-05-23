"""Built-in strategy registry for `qr backtest run --strategy <name>`.

Names map to `BaseStrategy` subclasses. Custom strategies that don't belong in
the repo are loaded from a file instead (`--strategy-file`, see
`quant_researcher.backtest.loader`). To add a built-in: drop a module here and
register it in `REGISTRY` (the keys also drive the CLI's "valid:" error list).
"""

from __future__ import annotations

from quant_researcher.backtest.strategies.bollinger_reversion import BollingerReversion
from quant_researcher.backtest.strategies.buy_and_hold import BuyAndHold
from quant_researcher.backtest.strategies.donchian_breakout import DonchianBreakout
from quant_researcher.backtest.strategies.macd_crossover import MACDCrossover
from quant_researcher.backtest.strategies.rsi_reversion import RSIReversion
from quant_researcher.backtest.strategies.sma_crossover import SMACrossover
from quant_researcher.engine.strategy.base import BaseStrategy

# All built-ins are single-symbol (the runner auto-injects symbols[0]).
# Multi-symbol rotation strategies are deferred until the engine's multi-symbol
# bar-alignment limitation is fixed upstream (see CLAUDE.md §13).
REGISTRY: dict[str, type[BaseStrategy]] = {
    "sma_crossover": SMACrossover,
    "buy_and_hold": BuyAndHold,
    "macd_crossover": MACDCrossover,
    "bollinger_reversion": BollingerReversion,
    "rsi_reversion": RSIReversion,
    "donchian_breakout": DonchianBreakout,
}


def get_strategy(name: str) -> type[BaseStrategy]:
    """Resolve a built-in strategy class by name, or raise with valid names."""
    try:
        return REGISTRY[name]
    except KeyError:
        valid = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown strategy '{name}' (valid: {valid})") from None


__all__ = [
    "REGISTRY",
    "BollingerReversion",
    "BuyAndHold",
    "DonchianBreakout",
    "MACDCrossover",
    "RSIReversion",
    "SMACrossover",
    "get_strategy",
]
