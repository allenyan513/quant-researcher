"""Built-in strategy registry for `qr backtest run --strategy <name>`.

Names map to `BaseStrategy` subclasses. Custom strategies that don't belong in
the repo are loaded from a file instead (`--strategy-file`, see
`quant_researcher.backtest.loader`). To add a built-in: drop a module here and
register it in `REGISTRY` (the keys also drive the CLI's "valid:" error list).
"""

from __future__ import annotations

from quant_researcher.backtest.strategies.sma_crossover import SMACrossover
from quant_researcher.engine.strategy.base import BaseStrategy

REGISTRY: dict[str, type[BaseStrategy]] = {
    "sma_crossover": SMACrossover,
}


def get_strategy(name: str) -> type[BaseStrategy]:
    """Resolve a built-in strategy class by name, or raise with valid names."""
    try:
        return REGISTRY[name]
    except KeyError:
        valid = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown strategy '{name}' (valid: {valid})") from None


__all__ = ["REGISTRY", "SMACrossover", "get_strategy"]
