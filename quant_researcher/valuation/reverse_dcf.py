"""Reverse DCF — solve for the stage-1 growth the current price implies.

Pure-Python (no DB), like `dcf.py`. Given the same inputs the forward DCF uses
plus a `target_price`, find the constant stage-1 growth rate `g` such that
`dcf_fcff(...).fair_value_per_share == target_price`. Per-share fair value is
monotonically increasing in `g`, so a bisection on `g` converges reliably — this
backs out "what growth is the market pricing in", the value-investor's mirror of
the forward DCF.
"""

from __future__ import annotations

from typing import Any

from quant_researcher.valuation.dcf import DCFError, dcf_fcff


def implied_growth(
    *,
    base_fcf: float,
    terminal_growth: float,
    wacc: float,
    target_price: float,
    n_years: int = 5,
    net_debt: float = 0.0,
    shares: float | None = None,
    lo: float = -0.50,
    hi: float = 1.00,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> dict[str, Any]:
    """Constant stage-1 growth that reproduces `target_price`, via bisection.

    Returns `{implied_growth, target_price, bracket, note}`. `implied_growth` is
    None when the price can't be matched inside `[lo, hi]` (the note says which
    side, with `implied_above` / `implied_below` flags) or when inputs are
    degenerate (no shares, non-positive base FCF, DCF undefined at the bracket).
    """
    if shares is None or shares <= 0:
        return _na("shares unavailable — cannot solve per-share", target_price, lo, hi)
    if base_fcf is None or base_fcf <= 0:
        return _na("base_fcf must be positive", target_price, lo, hi)

    def fv(g: float) -> float | None:
        try:
            return dcf_fcff(
                base_fcf=base_fcf,
                growth_rate=g,
                terminal_growth=terminal_growth,
                wacc=wacc,
                n_years=n_years,
                net_debt=net_debt,
                shares=shares,
            )["fair_value_per_share"]
        except DCFError:
            return None

    fv_lo, fv_hi = fv(lo), fv(hi)
    if fv_lo is None or fv_hi is None:
        return _na("DCF undefined at bracket (wacc <= terminal_growth?)", target_price, lo, hi)
    if target_price <= fv_lo:
        return _na(
            f"price at/below DCF value at g={lo:.0%} — decline already priced",
            target_price, lo, hi, implied_below=True,
        )
    if target_price >= fv_hi:
        return _na(
            f"price prices in growth above the {hi:.0%} bracket ceiling",
            target_price, lo, hi, implied_above=True,
        )

    # fv is increasing in g: keep `a` on the low-FV side, `b` on the high-FV side.
    a, b = lo, hi
    for _ in range(max_iter):
        mid = (a + b) / 2
        val = fv(mid)
        if val is None:  # defensive — shouldn't happen between valid endpoints
            return _na("DCF undefined mid-bracket", target_price, lo, hi)
        if abs(val - target_price) < tol * max(1.0, abs(target_price)):
            return {
                "implied_growth": mid,
                "target_price": target_price,
                "bracket": [lo, hi],
                "note": None,
            }
        if val < target_price:
            a = mid
        else:
            b = mid
    return {
        "implied_growth": (a + b) / 2,
        "target_price": target_price,
        "bracket": [lo, hi],
        "note": "max_iter reached (approximate)",
    }


def _na(
    note: str, target_price: float, lo: float, hi: float, **extra: Any
) -> dict[str, Any]:
    return {
        "implied_growth": None,
        "target_price": target_price,
        "bracket": [lo, hi],
        "note": note,
        **extra,
    }
