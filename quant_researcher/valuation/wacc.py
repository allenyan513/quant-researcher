"""WACC computation — v1 simple CAPM.

`simple_wacc(beta)` = RF + adj(β) × ERP, where `adj(β) = 2/3·β + 1/3` is
the standard Bloomberg adjustment that pulls raw OLS betas back toward 1
(empirically more predictive). Default RF = 4.5% (US 10Y treasury rough
average; FRED integration deferred), ERP = 5.5% (Damodaran-style mature
equity premium).

v1 intentionally collapses WACC to cost-of-equity — no debt-mix
adjustment. For most US large-caps the difference is < 1pp and the
simplification keeps the DCF inputs auditable. To extend, add
`cost_of_debt`, `tax_rate`, `debt_weight` parameters and compute
`wacc = w_e * coe + w_d * cod * (1 - t)`; the DCF call site already
accepts WACC as a scalar.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.models.profile import Profile

DEFAULT_RISK_FREE_RATE = 0.045
DEFAULT_EQUITY_RISK_PREMIUM = 0.055
DEFAULT_BETA_FALLBACK = 1.0  # if a Profile has no beta, treat as market


def bloomberg_adjusted_beta(raw_beta: float | None) -> float:
    """`adj β = 2/3 β + 1/3`. Falls back to 1.0 on missing input."""
    if raw_beta is None:
        return DEFAULT_BETA_FALLBACK
    return (2.0 / 3.0) * float(raw_beta) + (1.0 / 3.0)


def simple_wacc(
    beta: float | None,
    *,
    rf: float = DEFAULT_RISK_FREE_RATE,
    erp: float = DEFAULT_EQUITY_RISK_PREMIUM,
) -> float:
    """`rf + adj(β) × erp`. Collapses to cost-of-equity in v1 (no debt mix)."""
    return rf + bloomberg_adjusted_beta(beta) * erp


def wacc_for_symbol(
    session: Session,
    symbol: str,
    *,
    rf: float = DEFAULT_RISK_FREE_RATE,
    erp: float = DEFAULT_EQUITY_RISK_PREMIUM,
) -> tuple[float, dict[str, float | None]]:
    """Compute WACC for `symbol` using its stored beta.

    Returns `(wacc, breakdown)` so the caller can persist the inputs.
    """
    raw_beta = session.scalar(select(Profile.beta).where(Profile.symbol == symbol))
    adj_beta = bloomberg_adjusted_beta(raw_beta)
    wacc = rf + adj_beta * erp
    return wacc, {
        "rf": rf,
        "erp": erp,
        "raw_beta": float(raw_beta) if raw_beta is not None else None,
        "adj_beta": adj_beta,
        "wacc": wacc,
    }
