"""Relative valuation via sector-median multiples.

For each of P/E, EV/EBITDA, EV/Revenue we:
1. Compute the median multiple across the target's sector (peer set drawn
   from `profiles` joined to latest annual `financial_ratios`).
2. Apply it to the target's per-share/EBITDA/Revenue.
3. Return implied per-share value + upside vs current price.

Sector medians are computed at call time from the current warehouse — no
`sector_betas`-style cached table (D6: not needed yet). If the sector has
fewer than 2 peers with the metric, the relevant multiple returns None
(too thin to be meaningful).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.models.profile import Profile
from quant_researcher.research.sector_classifier import classify_stock_type, net_revenue
from quant_researcher.valuation.helpers import (
    latest_close,
    latest_ebitda,
    latest_income_statement,
    latest_market_cap,
    net_debt,
    sector_for_symbol,
    sector_peer_median,
    shares_outstanding,
)


def _safe_div(num: float | None, denom: float | None) -> float | None:
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


# EV/EBITDA = enterprise_value / EBITDA assumes "debt" on the balance sheet
# is corporate funding to subtract from EV. For banks the largest liability
# bucket is deposits / wholesale funding — customer money, not capital
# structure — so net_debt blows up and equity = EV − net_debt flips
# negative, producing a nonsense per-share value. Same shape for REITs, where
# the right multiple is P/FFO or P/TBV, not EV/EBITDA.
_EVEBITDA_SKIP_SECTORS = frozenset({"Financial Services", "Real Estate"})


def pe_implied_price(session: Session, symbol: str, sector: str) -> dict[str, Any]:
    peer_median = sector_peer_median(session, sector, "pe_ratio")
    inc = latest_income_statement(session, symbol)
    eps = inc.eps_diluted if inc else None
    implied = peer_median * eps if peer_median is not None and eps is not None else None
    return {
        "peer_median_pe": peer_median,
        "eps_diluted": eps,
        "implied_price": implied,
    }


def ev_ebitda_implied_price(
    session: Session, symbol: str, sector: str
) -> dict[str, Any]:
    if sector in _EVEBITDA_SKIP_SECTORS:
        return {
            "peer_median_ev_ebitda": None,
            "ebitda": None,
            "implied_enterprise_value": None,
            "implied_equity_value": None,
            "shares": None,
            "implied_price": None,
            "note": (
                f"EV/EBITDA n/a for {sector}: subtracting deposits / "
                "funding liabilities from EV yields a nonsense equity value."
            ),
        }
    peer_median = sector_peer_median(session, sector, "ev_to_ebitda")
    ebitda = latest_ebitda(session, symbol)
    shares = shares_outstanding(session, symbol)
    debt = net_debt(session, symbol) or 0.0
    implied_ev = peer_median * ebitda if peer_median is not None and ebitda is not None else None
    implied_equity = implied_ev - debt if implied_ev is not None else None
    implied_price = _safe_div(implied_equity, shares)
    return {
        "peer_median_ev_ebitda": peer_median,
        "ebitda": ebitda,
        "implied_enterprise_value": implied_ev,
        "implied_equity_value": implied_equity,
        "shares": shares,
        "implied_price": implied_price,
    }


def ev_revenue_implied_price(
    session: Session, symbol: str, sector: str
) -> dict[str, Any]:
    peer_median = sector_peer_median(session, sector, "price_to_sales")
    # `price_to_sales` ≈ market_cap / revenue, so implied market_cap
    # = peer_median × revenue, then per-share = implied_mcap / shares.
    # Bank-aware (issue #36): FMP's `revenue` for financials is gross
    # (interestIncome + non-interest income); peer-median P/S is
    # computed against analysts' net revenue. Compare net-vs-net.
    inc = latest_income_statement(session, symbol)
    p = session.get(Profile, symbol)
    stock_type = classify_stock_type(
        p.sector if p else None, p.industry if p else None
    )
    revenue = net_revenue(inc, stock_type) if inc is not None else None
    shares = shares_outstanding(session, symbol)
    if peer_median is not None and revenue is not None:
        implied_mcap = peer_median * revenue
    else:
        implied_mcap = None
    implied_price = _safe_div(implied_mcap, shares)
    return {
        "peer_median_ps": peer_median,
        "revenue": revenue,
        "implied_market_cap": implied_mcap,
        "shares": shares,
        "implied_price": implied_price,
    }


def value_via_multiples(session: Session, symbol: str) -> dict[str, Any]:
    """Run all three peer-median multiples for `symbol` and aggregate."""
    sector = sector_for_symbol(session, symbol)
    if not sector:
        return {
            "symbol": symbol,
            "sector": None,
            "note": "no sector recorded for symbol",
            "models": {},
            "fair_value_per_share": None,
        }

    pe_result = pe_implied_price(session, symbol, sector)
    ev_ebitda_result = ev_ebitda_implied_price(session, symbol, sector)
    ev_rev_result = ev_revenue_implied_price(session, symbol, sector)

    # Exclude None AND non-positive implied prices from the cross-component
    # average. A negative or zero per-share value never represents a real
    # fair price; including one would poison the blend. Belt-and-suspenders
    # alongside the sector gate on EV/EBITDA above.
    implied_prices = [
        r["implied_price"]
        for r in (pe_result, ev_ebitda_result, ev_rev_result)
        if r["implied_price"] is not None and r["implied_price"] > 0
    ]
    avg_implied = (
        sum(implied_prices) / len(implied_prices) if implied_prices else None
    )

    current = latest_close(session, symbol) or latest_market_cap(session, symbol)
    upside = None
    if avg_implied is not None and current and current > 0:
        upside = avg_implied / current - 1

    return {
        "symbol": symbol,
        "sector": sector,
        "models": {
            "pe": pe_result,
            "ev_ebitda": ev_ebitda_result,
            "ev_revenue": ev_rev_result,
        },
        "fair_value_per_share": avg_implied,
        "current_price": latest_close(session, symbol),
        "upside_pct": upside,
    }
