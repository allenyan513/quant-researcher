"""Discounted Cash Flow (FCFF) with Gordon growth terminal value.

Pure-Python — takes already-loaded inputs (historical FCF list, WACC,
shares, net_debt) and returns a `DCFResult` dict. Data fetching lives in
`engine.py`; this module is unit-testable without a DB.

v1 supports only the **Gordon growth terminal** (`TV = FCF_N × (1 + g_t)
/ (WACC − g_t)`). The implementation-plan also lists "EBITDA exit
multiple" — left as a future toggle (`terminal_method="exit_multiple"`).

Sensitivity: 5×5 grid over `(growth_rate, wacc)` is the workhorse; both
axes are configurable. Returns a list of rows so JSON serialization
preserves order; `cell(g_index, w_index)` indexing is documented.
"""

from __future__ import annotations

import statistics
from typing import Any


class DCFError(ValueError):
    """Raised when inputs make the DCF undefined (e.g. WACC ≤ terminal_growth)."""


def dcf_fcff(
    *,
    base_fcf: float,
    growth_rate: float,
    terminal_growth: float,
    wacc: float,
    n_years: int = 5,
    net_debt: float = 0.0,
    shares: float | None = None,
) -> dict[str, Any]:
    """Single-scenario DCF-FCFF intrinsic value.

    Parameters are all required scalars — no defaults masquerading as
    assumptions. Returns a dict with `enterprise_value`, `equity_value`,
    `fair_value_per_share` (None if shares missing), `projected_fcf` per
    year, `pv_per_year`, `terminal_value`, `terminal_pv`.
    """
    if wacc <= terminal_growth:
        raise DCFError(
            f"wacc ({wacc:.4f}) must exceed terminal_growth ({terminal_growth:.4f}); "
            "Gordon formula divides by (wacc - g)."
        )
    if n_years < 1:
        raise DCFError(f"n_years must be >= 1, got {n_years}")

    projected: list[float] = []
    pvs: list[float] = []
    for year in range(1, n_years + 1):
        fcf = base_fcf * (1 + growth_rate) ** year
        pv = fcf / (1 + wacc) ** year
        projected.append(fcf)
        pvs.append(pv)

    # Gordon growth terminal value at end of year N (in year N dollars), then
    # discounted back N years to present.
    terminal_value = projected[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    terminal_pv = terminal_value / (1 + wacc) ** n_years

    enterprise_value = sum(pvs) + terminal_pv
    equity_value = enterprise_value - net_debt
    per_share = equity_value / shares if shares and shares > 0 else None

    return {
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "fair_value_per_share": per_share,
        "projected_fcf": projected,
        "pv_per_year": pvs,
        "terminal_value": terminal_value,
        "terminal_pv": terminal_pv,
        "assumptions": {
            "base_fcf": base_fcf,
            "growth_rate": growth_rate,
            "terminal_growth": terminal_growth,
            "wacc": wacc,
            "n_years": n_years,
            "net_debt": net_debt,
            "shares": shares,
        },
    }


def sensitivity_5x5(
    *,
    base_fcf: float,
    growth_rates: list[float],
    waccs: list[float],
    terminal_growth: float,
    n_years: int = 5,
    net_debt: float = 0.0,
    shares: float | None = None,
) -> dict[str, Any]:
    """Per-share fair value across a 2D `(growth, wacc)` grid.

    Returns `{growth_axis, wacc_axis, grid}` where `grid[i][j]` corresponds
    to `(growth_rates[i], waccs[j])`. Cells where the DCF is undefined
    (`wacc <= terminal_growth`) are stored as `None`.
    """
    grid: list[list[float | None]] = []
    for g in growth_rates:
        row: list[float | None] = []
        for w in waccs:
            try:
                cell = dcf_fcff(
                    base_fcf=base_fcf,
                    growth_rate=g,
                    terminal_growth=terminal_growth,
                    wacc=w,
                    n_years=n_years,
                    net_debt=net_debt,
                    shares=shares,
                )
                row.append(cell["fair_value_per_share"])
            except DCFError:
                row.append(None)
        grid.append(row)
    return {
        "growth_axis": list(growth_rates),
        "wacc_axis": list(waccs),
        "grid": grid,
        "terminal_growth": terminal_growth,
    }


def smoothed_base_fcf(historical: list[float], window: int = 3) -> float | None:
    """Median of the most recent `window` FCF values, to dampen one-off years.

    Returns None if the series is empty. Falls back to whatever's there if
    `len(historical) < window`.
    """
    if not historical:
        return None
    tail = historical[-window:]
    return float(statistics.median(tail))


def default_growth_from_history(
    historical: list[float], floor: float = -0.10, cap: float = 0.20
) -> float | None:
    """CAGR of FCF over the provided history, clipped to `[floor, cap]`.

    Returns None if we can't compute (≤1 point, non-positive start). The
    clip prevents wild extrapolation when one year is anomalous.
    """
    if len(historical) < 2:
        return None
    start, end = historical[0], historical[-1]
    # CAGR is undefined for non-positive endpoints: a negative `end` (e.g. FCF
    # swung negative) makes `(end/start)` negative and `negative ** (1/years)`
    # returns a COMPLEX number silently (not a ValueError) — which then crashes
    # downstream `min(cap, cagr)`. Guard both ends.
    if start <= 0 or end <= 0:
        return None
    years = len(historical) - 1
    try:
        cagr = (end / start) ** (1 / years) - 1
    except (ValueError, ZeroDivisionError):
        return None
    return max(floor, min(cap, cagr))
