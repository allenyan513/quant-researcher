"""PEG / Lynch fair-P/E valuation.

PEG = P/E ÷ (earnings growth %, in whole percent). Peter Lynch's rule of
thumb is that a "fair" P/E equals the earnings growth rate in percent —
so a company growing earnings at 15%/yr is "fair" at a P/E of 15.

We derive the growth rate from up-to-5-year net_income CAGR
(`helpers.earnings_growth_rate`). v1 doesn't use forward analyst
estimates for PEG (could be wired later via `analyst_estimates`); that
keeps the snapshot self-contained.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.valuation.helpers import (
    earnings_growth_rate,
    latest_close,
    latest_income_statement,
    latest_ratios,
)


def peg_value(
    pe: float | None, growth_rate: float | None, *, eps: float | None = None
) -> dict[str, Any]:
    """PEG ratio + Lynch "fair P/E" implied price.

    `growth_rate` is decimal (0.15 for 15%); we convert to percent for the
    standard PEG formula. Returns None fields gracefully when inputs are
    missing.
    """
    if pe is None or growth_rate is None or growth_rate <= 0:
        return {
            "pe": pe,
            "growth_rate": growth_rate,
            "peg_ratio": None,
            "fair_pe": None,
            "fair_value_per_share": None,
            "note": "missing pe or growth_rate (or non-positive growth)",
        }
    growth_pct = growth_rate * 100
    peg = pe / growth_pct
    fair_pe = growth_pct  # Lynch heuristic
    fair_price = fair_pe * eps if eps is not None else None
    return {
        "pe": pe,
        "growth_rate": growth_rate,
        "growth_pct": growth_pct,
        "peg_ratio": peg,
        "fair_pe": fair_pe,
        "fair_value_per_share": fair_price,
        "interpretation": _interpret_peg(peg),
    }


def _interpret_peg(peg: float) -> str:
    if peg < 0.5:
        return "deeply_undervalued"
    if peg < 1.0:
        return "undervalued"
    if peg < 1.5:
        return "fairly_priced"
    return "overvalued"


def value_via_peg(session: Session, symbol: str) -> dict[str, Any]:
    """Pull pe + growth + eps from the warehouse and call `peg_value`."""
    ratios = latest_ratios(session, symbol)
    pe = ratios.pe_ratio if ratios else None
    growth = earnings_growth_rate(session, symbol, n=5)
    inc = latest_income_statement(session, symbol)
    eps = inc.eps_diluted if inc else None
    out = peg_value(pe, growth, eps=eps)
    out["current_price"] = latest_close(session, symbol)
    if out["fair_value_per_share"] is not None and out["current_price"]:
        out["upside_pct"] = (
            out["fair_value_per_share"] / out["current_price"] - 1
        )
    else:
        out["upside_pct"] = None
    return out
