"""Sector → SPDR sector-ETF mapping for benchmark tracking.

Maps FMP-style sector strings to the standard SPDR sector ETFs (XL*) so
the decision tracker can compute "alpha vs sector". Unknown sectors fall
back to SPY (`market_etf`). Mapping is intentionally small + hardcoded —
it's the SPDR universe, doesn't change.
"""

from __future__ import annotations

MARKET_ETF = "SPY"

# Keys are the lowercased sector strings as FMP reports them.
SECTOR_ETF: dict[str, str] = {
    "technology": "XLK",
    "financial services": "XLF",
    "financials": "XLF",
    "healthcare": "XLV",
    "health care": "XLV",
    "energy": "XLE",
    "consumer cyclical": "XLY",
    "consumer discretionary": "XLY",
    "consumer defensive": "XLP",
    "consumer staples": "XLP",
    "industrials": "XLI",
    "industrial": "XLI",
    "communication services": "XLC",
    "utilities": "XLU",
    "real estate": "XLRE",
    "basic materials": "XLB",
    "materials": "XLB",
}


def etf_for_sector(sector: str | None) -> str:
    """Return the SPDR sector ETF for `sector`. Fallback = SPY."""
    if not sector:
        return MARKET_ETF
    return SECTOR_ETF.get(sector.strip().lower(), MARKET_ETF)
