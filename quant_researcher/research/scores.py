"""Quality / forensic scores — pure functions, no DB (mirrors valuation/dcf.py).

Each function takes plain numbers / small dicts already assembled by the caller
(the research bundler) and returns a result dict. Missing inputs degrade
gracefully: a component that can't be computed is left out of the score and
named in `missing`, so a partial score is honest rather than silently wrong.

Definitions:
* **Piotroski F-score** (0–9): nine binary fundamental signals across
  profitability, leverage/liquidity, and operating efficiency. Higher = stronger.
* **Altman Z''-score**: the 4-factor cross-industry revision (drops sales/assets,
  uses *book* equity), appropriate for non-manufacturers / tech — NOT the original
  5-factor manufacturing Z. Zones: > 2.6 safe · 1.1–2.6 grey · < 1.1 distress.
  EBIT is approximated by operating income (we don't promote a separate EBIT col).
"""

from __future__ import annotations

from typing import Any


def fcf_conversion(free_cash_flow: float | None, net_income: float | None) -> float | None:
    """Free cash flow / net income. > 1 ≈ earnings well-backed by cash."""
    if free_cash_flow is None or not net_income:
        return None
    return free_cash_flow / net_income


def accruals_ratio(
    net_income: float | None,
    operating_cash_flow: float | None,
    total_assets: float | None,
) -> float | None:
    """Sloan-style accruals: (NI − CFO) / total assets.

    Lower (more negative) = earnings backed by cash = higher quality; a large
    positive = earnings running ahead of cash, an earnings-quality red flag.
    """
    if net_income is None or operating_cash_flow is None or not total_assets:
        return None
    return (net_income - operating_cash_flow) / total_assets


def roic_wacc_spread(roic: float | None, wacc: float | None) -> float | None:
    """ROIC − WACC: the core value-creation test (positive = creating value)."""
    if roic is None or wacc is None:
        return None
    return roic - wacc


def trend(values: list[float | None]) -> dict[str, Any] | None:
    """Direction of a series given oldest→newest values (None-tolerant).

    Returns first/last/change/direction over the non-None points, or None when
    fewer than two are available.
    """
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return None
    change = pts[-1] - pts[0]
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    return {
        "first": pts[0],
        "last": pts[-1],
        "change": change,
        "direction": direction,
        "n": len(pts),
    }


def piotroski_f(curr: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any]:
    """9-point Piotroski F-score from two consecutive fiscal years.

    `curr` / `prev` carry: net_income, total_assets, operating_cash_flow,
    long_term_debt, current_assets, current_liabilities, gross_profit, revenue,
    shares. Any leg whose inputs are missing is scored None and listed in
    `missing`; `max_possible` is the count of legs that could be computed.
    """
    components: dict[str, int | None] = {}
    missing: list[str] = []

    roa_c = _ratio(curr, "net_income", "total_assets")
    roa_p = _ratio(prev, "net_income", "total_assets")
    cfo_c, ni_c = curr.get("operating_cash_flow"), curr.get("net_income")
    _award(components, missing, "roa_positive", None if roa_c is None else int(roa_c > 0))
    _award(components, missing, "cfo_positive", None if cfo_c is None else int(cfo_c > 0))
    _award(components, missing, "delta_roa", _gt(roa_c, roa_p))
    _award(components, missing, "accruals", _gt(cfo_c, ni_c))

    lev_c = _ratio(curr, "long_term_debt", "total_assets")
    lev_p = _ratio(prev, "long_term_debt", "total_assets")
    cr_c = _ratio(curr, "current_assets", "current_liabilities")
    cr_p = _ratio(prev, "current_assets", "current_liabilities")
    sh_c, sh_p = curr.get("shares"), prev.get("shares")
    no_dilution = None if sh_c is None or sh_p is None else int(sh_c <= sh_p)
    _award(components, missing, "delta_leverage", _lt(lev_c, lev_p))
    _award(components, missing, "delta_liquidity", _gt(cr_c, cr_p))
    _award(components, missing, "no_dilution", no_dilution)

    gm_c, gm_p = _ratio(curr, "gross_profit", "revenue"), _ratio(prev, "gross_profit", "revenue")
    at_c, at_p = _ratio(curr, "revenue", "total_assets"), _ratio(prev, "revenue", "total_assets")
    _award(components, missing, "delta_margin", _gt(gm_c, gm_p))
    _award(components, missing, "delta_turnover", _gt(at_c, at_p))

    awarded = [v for v in components.values() if v is not None]
    return {
        "score": sum(awarded),
        "max_possible": len(awarded),
        "components": components,
        "missing": missing,
    }


def altman_z(
    *,
    current_assets: float | None,
    current_liabilities: float | None,
    total_assets: float | None,
    retained_earnings: float | None,
    operating_income: float | None,
    total_equity: float | None,
    total_liabilities: float | None,
) -> dict[str, Any] | None:
    """Altman Z''-score (4-factor, cross-industry). None if any input is missing.

    Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4, where
    X1 = working capital / TA, X2 = retained earnings / TA,
    X3 = EBIT(≈operating income) / TA, X4 = book equity / total liabilities.
    """
    if not total_assets or not total_liabilities:
        return None
    if any(v is None for v in (current_assets, current_liabilities, retained_earnings,
                               operating_income, total_equity)):
        return None
    x1 = (current_assets - current_liabilities) / total_assets  # type: ignore[operator]
    x2 = retained_earnings / total_assets  # type: ignore[operator]
    x3 = operating_income / total_assets  # type: ignore[operator]
    x4 = total_equity / total_liabilities  # type: ignore[operator]
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    zone = "safe" if z > 2.6 else "distress" if z < 1.1 else "grey"
    return {
        "z_score": z,
        "zone": zone,
        "variant": "Z''(4-factor, cross-industry)",
        "components": {"x1_wc_ta": x1, "x2_re_ta": x2, "x3_ebit_ta": x3, "x4_eq_tl": x4},
    }


# ----- internals ------------------------------------------------------------


def _ratio(d: dict[str, Any], num: str, den: str) -> float | None:
    a, b = d.get(num), d.get(den)
    if a is None or not b:
        return None
    return a / b


def _gt(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else int(a > b)


def _lt(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else int(a < b)


def _award(
    components: dict[str, int | None], missing: list[str], name: str, value: int | None
) -> None:
    components[name] = value
    if value is None:
        missing.append(name)
