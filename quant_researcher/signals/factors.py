"""Built-in factor registry for `qr signal research --factor <name>`.

Two kinds:
- `fundamental` — reuses `screen.expression.FIELDS` (a factor name maps to a
  `financial_ratios` column); values are pulled point-in-time in `panel.py`.
- `price` — computed from a `PriceSeries` at a rebalance date (momentum etc.).

`direction` is the EXPECTED sign of correlation with forward return, used only
for REPORTING (aligning the long-short spread); never to flip raw IC.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import numpy as np

from quant_researcher.screen.expression import FIELDS
from quant_researcher.signals.panel import PriceSeries, forward_return


class FactorError(ValueError):
    """Unknown factor name / non-ratio fundamental field."""


@dataclass(frozen=True)
class FactorSpec:
    name: str
    kind: str  # "fundamental" | "price"
    description: str
    direction: int  # +1 higher→higher return, -1 higher→lower, 0 agnostic
    ratio_col: str | None = None  # fundamental: financial_ratios column
    price_fn: Callable[[PriceSeries, date], float | None] | None = None


# Expected direction for fundamental factors (reporting only).
_FUNDAMENTAL_DIRECTION: dict[str, int] = {
    "roe": 1, "roa": 1, "roic": 1, "fcf_yield": 1, "earnings_yield": 1,
    "gross_margin": 1, "operating_margin": 1, "net_margin": 1,
    "pe": -1, "pb": -1, "ps": -1, "peg": -1, "ev_ebitda": -1,
    "debt_equity": -1, "market_cap": -1,
}


def _ratio_col(field_name: str) -> str:
    """Map a screen FIELDS name → its financial_ratios column (validate source)."""
    spec = FIELDS[field_name]
    table, _, col = spec.partition(".")
    if table != "financial_ratios":
        raise FactorError(f"{field_name!r} is not a financial_ratios factor")
    return col


# ----- price factors --------------------------------------------------------


def _momentum(series: PriceSeries, anchor: date, old: int, recent: int) -> float | None:
    idx = series.index_on_or_before(anchor)
    if idx is None:
        return None
    return forward_return(series.price_at_offset(idx, old), series.price_at_offset(idx, recent))


def _momentum_12_1(series: PriceSeries, anchor: date) -> float | None:
    """12-1 momentum: return from ~252 to ~21 trading days ago (skip last month)."""
    return _momentum(series, anchor, old=252, recent=21)


def _momentum_6_1(series: PriceSeries, anchor: date) -> float | None:
    return _momentum(series, anchor, old=126, recent=21)


def _reversal_1m(series: PriceSeries, anchor: date) -> float | None:
    """1-month reversal: negative of the last ~21 trading-day return."""
    idx = series.index_on_or_before(anchor)
    if idx is None:
        return None
    r = forward_return(series.price_at_offset(idx, 21), series.price_at_offset(idx, 0))
    return None if r is None else -r


def _realized_vol_3m(series: PriceSeries, anchor: date) -> float | None:
    """Annualized stdev of ~63 trailing daily log returns (low-vol anomaly → -1)."""
    idx = series.index_on_or_before(anchor)
    if idx is None or idx < 63:
        return None
    window = series.prices[idx - 63 : idx + 1]
    window = window[~np.isnan(window)]
    if len(window) < 30:
        return None
    log_ret = np.diff(np.log(window))
    if len(log_ret) < 2:
        return None
    return float(np.std(log_ret, ddof=1) * np.sqrt(252))


_PRICE_FACTORS: list[FactorSpec] = [
    FactorSpec("momentum_12_1", "price", "12-1 month price momentum", 1,
               price_fn=_momentum_12_1),
    FactorSpec("momentum_6_1", "price", "6-1 month price momentum", 1,
               price_fn=_momentum_6_1),
    FactorSpec("reversal_1m", "price", "1-month reversal (neg recent return)", 1,
               price_fn=_reversal_1m),
    FactorSpec("realized_vol_3m", "price", "3-month annualized realized vol", -1,
               price_fn=_realized_vol_3m),
]


def _build_registry() -> dict[str, FactorSpec]:
    registry: dict[str, FactorSpec] = {}
    # fundamental factors from the screen FIELDS financial_ratios entries
    for name, spec in FIELDS.items():
        if not spec.startswith("financial_ratios."):
            continue
        registry[name] = FactorSpec(
            name=name,
            kind="fundamental",
            description=f"financial_ratios.{_ratio_col(name)} (point-in-time)",
            direction=_FUNDAMENTAL_DIRECTION.get(name, 0),
            ratio_col=_ratio_col(name),
        )
    for spec in _PRICE_FACTORS:
        registry[spec.name] = spec
    return registry


REGISTRY: dict[str, FactorSpec] = _build_registry()


def get_factor(name: str) -> FactorSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        valid = ", ".join(sorted(REGISTRY))
        raise FactorError(f"unknown factor {name!r} (valid: {valid})") from None


def list_factors() -> list[dict[str, object]]:
    """[{name, kind, description, direction}] for `qr signal factors`."""
    return [
        {"name": s.name, "kind": s.kind, "description": s.description, "direction": s.direction}
        for s in sorted(REGISTRY.values(), key=lambda s: (s.kind, s.name))
    ]
