"""Pure-function quality / forensic scores (research/scores.py)."""

from __future__ import annotations

import pytest

from quant_researcher.research import scores


def test_fcf_conversion() -> None:
    assert scores.fcf_conversion(100.0, 80.0) == pytest.approx(1.25)
    assert scores.fcf_conversion(100.0, 0) is None  # zero NI guarded
    assert scores.fcf_conversion(None, 80.0) is None


def test_accruals_ratio() -> None:
    # CFO ahead of NI → negative accruals (higher earnings quality)
    assert scores.accruals_ratio(100.0, 120.0, 400.0) == pytest.approx(-0.05)
    assert scores.accruals_ratio(100.0, 120.0, 0) is None
    assert scores.accruals_ratio(None, 120.0, 400.0) is None


def test_roic_wacc_spread() -> None:
    assert scores.roic_wacc_spread(0.20, 0.09) == pytest.approx(0.11)
    assert scores.roic_wacc_spread(None, 0.09) is None
    assert scores.roic_wacc_spread(0.20, None) is None


def test_trend() -> None:
    up = scores.trend([1.0, 2.0, 3.0])
    assert up["direction"] == "up"
    assert up["change"] == pytest.approx(2.0)
    assert up["n"] == 3
    assert scores.trend([3.0, 1.0])["direction"] == "down"
    assert scores.trend([2.0, 2.0])["direction"] == "flat"
    assert scores.trend([None, 5.0]) is None  # <2 non-None points
    assert scores.trend([1.0, None, 4.0])["change"] == pytest.approx(3.0)  # None-tolerant


def test_piotroski_full_nine() -> None:
    prev = dict(net_income=80, total_assets=400, operating_cash_flow=90,
                long_term_debt=120, current_assets=150, current_liabilities=100,
                gross_profit=160, revenue=350, shares=20)
    curr = dict(net_income=100, total_assets=420, operating_cash_flow=120,
                long_term_debt=110, current_assets=180, current_liabilities=100,
                gross_profit=200, revenue=420, shares=19)
    r = scores.piotroski_f(curr, prev)
    assert r["score"] == 9
    assert r["max_possible"] == 9
    assert r["missing"] == []


def test_piotroski_partial_marks_missing() -> None:
    # Only profitability inputs present → 4 computable legs, 5 named missing.
    prev = dict(net_income=80, total_assets=400, operating_cash_flow=90)
    curr = dict(net_income=100, total_assets=400, operating_cash_flow=120)
    r = scores.piotroski_f(curr, prev)
    assert r["max_possible"] == 4
    assert r["score"] == 4
    for leg in ("delta_leverage", "delta_liquidity", "no_dilution",
                "delta_margin", "delta_turnover"):
        assert leg in r["missing"]


def test_altman_z_safe_zone() -> None:
    r = scores.altman_z(
        current_assets=180, current_liabilities=100, total_assets=420,
        retained_earnings=200, operating_income=130, total_equity=250,
        total_liabilities=170,
    )
    assert r is not None
    assert r["z_score"] == pytest.approx(6.426, abs=0.01)
    assert r["zone"] == "safe"
    assert r["variant"].startswith("Z''")


def test_altman_z_none_on_missing_or_zero() -> None:
    base = dict(current_assets=180, current_liabilities=100, total_assets=420,
                retained_earnings=200, operating_income=130, total_equity=250,
                total_liabilities=170)
    assert scores.altman_z(**{**base, "retained_earnings": None}) is None
    assert scores.altman_z(**{**base, "total_liabilities": 0}) is None  # divide guard
