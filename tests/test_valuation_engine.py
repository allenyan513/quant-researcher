"""Valuation engine — orchestration + persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.valuation import ValuationSnapshot
from quant_researcher.valuation.engine import VALID_MODELS, value_company


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _seed_full_company(session: Session, sym: str = "AAPL") -> None:
    """A reasonably complete fake company so every model can run."""
    session.add(
        Profile(
            symbol=sym,
            sector="Technology",
            beta=1.2,
            raw={"mktCap": 3e12},
            known_at=datetime.now(UTC),
        )
    )
    # Five years of FY income statements with steady growth.
    for i in range(5):
        session.add(
            IncomeStatement(
                symbol=sym,
                period="FY",
                fiscal_date=date(2020 + i, 9, 30),
                net_income=1e10 * (1.08**i),
                eps_diluted=5.0 * (1.08**i),
                operating_income=1.5e10 * (1.08**i),
                revenue=5e10 * (1.08**i),
                known_at=datetime.now(UTC),
            )
        )
        session.add(
            CashFlow(
                symbol=sym,
                period="FY",
                fiscal_date=date(2020 + i, 9, 30),
                free_cash_flow=8e9 * (1.08**i),
                capital_expenditure=-2e9 * (1.08**i),
                known_at=datetime.now(UTC),
            )
        )
    session.add(
        BalanceSheet(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            short_term_debt=1e10,
            long_term_debt=5e10,
            cash_and_equivalents=4e10,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        FinancialRatios(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=28.0,
            ev_to_ebitda=22.0,
            price_to_sales=8.0,
            known_at=datetime.now(UTC),
        )
    )
    # Add 2 peers in same sector so peer median is computable.
    for peer_sym, peer_pe in (("MSFT", 30.0), ("NVDA", 35.0)):
        session.add(
            Profile(
                symbol=peer_sym,
                sector="Technology",
                beta=1.1,
                raw={"mktCap": 2e12},
                known_at=datetime.now(UTC),
            )
        )
        session.add(
            FinancialRatios(
                symbol=peer_sym,
                period="FY",
                fiscal_date=date(2024, 9, 30),
                pe_ratio=peer_pe,
                ev_to_ebitda=20.0,
                price_to_sales=10.0,
                known_at=datetime.now(UTC),
            )
        )
    session.add(DailyPrice(symbol=sym, trade_date=date(2024, 9, 30), close=200.0))
    session.commit()


# ----- happy path ---------------------------------------------------------


def test_value_company_all_models(session: Session) -> None:
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="all")
    assert set(out["models"].keys()) == {"dcf", "peg", "multiples", "scenario"}
    # At least DCF and multiples should produce a number.
    assert out["models"]["dcf"]["fair_value_per_share"] is not None
    assert out["models"]["multiples"]["fair_value_per_share"] is not None
    assert out["models"]["scenario"]["fair_value_per_share"] is not None
    # DCF now carries a reverse-DCF block.
    assert "reverse" in out["models"]["dcf"]
    # Aggregate mean exists.
    assert out["fair_value_per_share_mean"] is not None


def test_value_company_single_model(session: Session) -> None:
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="dcf")
    assert set(out["models"].keys()) == {"dcf"}
    assert "fair_value_per_share" in out["models"]["dcf"]


def test_value_company_invalid_model(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown model"):
        value_company(session, "AAPL", model="xyz")


def test_value_company_persists_snapshots(session: Session) -> None:
    _seed_full_company(session)
    value_company(session, "AAPL", model="all")
    session.commit()

    rows = list(session.scalars(select(ValuationSnapshot)))
    assert len(rows) == 4  # one per model (incl. scenario)
    model_types = {r.model_type for r in rows}
    assert model_types == {"dcf", "peg", "multiples", "scenario"}
    # The dcf row should have a sensitivity grid.
    dcf_row = next(r for r in rows if r.model_type == "dcf")
    assert dcf_row.sensitivity is not None
    assert dcf_row.fair_value_per_share is not None


def test_value_company_no_fcf_dcf_returns_none(session: Session) -> None:
    # No cashflow rows → DCF can't run.
    session.add(
        Profile(symbol="X", sector="Tech", beta=1.0, raw={}, known_at=datetime.now(UTC))
    )
    session.commit()
    out = value_company(session, "X", model="dcf")
    assert out["models"]["dcf"]["fair_value_per_share"] is None
    assert "history" in out["models"]["dcf"]


def test_value_company_assumptions_override(session: Session) -> None:
    _seed_full_company(session)
    # Pass extreme growth and tight WACC to verify they propagate through.
    out = value_company(
        session,
        "AAPL",
        model="dcf",
        assumptions={
            "growth_rate": 0.10,
            "terminal_growth": 0.03,
            "wacc": 0.08,
            "n_years": 7,
        },
    )
    assumptions = out["models"]["dcf"]["core"]["assumptions"]
    assert assumptions["growth_rate"] == 0.10
    assert assumptions["wacc"] == 0.08
    assert assumptions["n_years"] == 7


def test_value_company_scenario_model(session: Session) -> None:
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="scenario")
    assert set(out["models"].keys()) == {"scenario"}
    sc = out["models"]["scenario"]
    assert sc["fair_value_per_share"] is not None
    assert set(sc["scenarios"].keys()) == {"bear", "base", "bull"}
    assert sc["weight_used"] == pytest.approx(1.0)


def test_dcf_includes_reverse_block(session: Session) -> None:
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="dcf")
    rev = out["models"]["dcf"]["reverse"]
    assert rev is not None
    assert "assumed_growth" in rev  # expectations-gap context present


def test_scenario_excluded_from_cross_model_mean(session: Session) -> None:
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="all")
    independent = [
        out["models"][m]["fair_value_per_share"]
        for m in ("dcf", "peg", "multiples")
        if out["models"][m]["fair_value_per_share"] is not None
    ]
    expected = sum(independent) / len(independent)
    assert out["fair_value_per_share_mean"] == pytest.approx(expected)


def test_valid_models_constant() -> None:
    assert "dcf" in VALID_MODELS
    assert "peg" in VALID_MODELS
    assert "multiples" in VALID_MODELS
    assert "scenario" in VALID_MODELS
    assert "all" in VALID_MODELS


# ----- forward-EPS growth source threading -------------------------------


def test_dcf_uses_forward_eps_growth_when_available(session: Session) -> None:
    # _seed_full_company doesn't seed AnalystEstimate, so the existing
    # tests exercise the historical-FCF-CAGR fallback. Here we seed forward
    # consensus and assert the DCF engine routes through it.
    _seed_full_company(session)
    today = date.today()
    for i, eps in enumerate([6.5, 7.15, 7.865]):  # ~10%/yr forward CAGR
        session.add(
            AnalystEstimate(
                symbol="AAPL",
                period="FY",
                fiscal_date=today + timedelta(days=365 * (i + 1)),
                eps_avg=eps,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    out = value_company(session, "AAPL", model="dcf")
    assert out["models"]["dcf"]["growth_source"] == "forward_consensus"


def test_dcf_falls_back_to_historical_fcf_when_no_estimates(
    session: Session,
) -> None:
    # No AnalystEstimate rows → historical FCF CAGR remains the source.
    _seed_full_company(session)
    out = value_company(session, "AAPL", model="dcf")
    assert out["models"]["dcf"]["growth_source"] == "historical_fcf_cagr"


def test_dcf_user_override_growth_wins_over_forward(session: Session) -> None:
    # User-supplied assumption is the top of the priority order — analyst
    # consensus is informational, the user is authoritative.
    _seed_full_company(session)
    today = date.today()
    for i, eps in enumerate([6.5, 7.15, 7.865]):
        session.add(
            AnalystEstimate(
                symbol="AAPL",
                period="FY",
                fiscal_date=today + timedelta(days=365 * (i + 1)),
                eps_avg=eps,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    out = value_company(
        session, "AAPL", model="dcf", assumptions={"growth_rate": 0.05}
    )
    assert out["models"]["dcf"]["growth_source"] == "user_override"


def test_dcf_growth_source_default_fallback_labeled_correctly(
    session: Session,
) -> None:
    # When both forward consensus and historical FCF CAGR are unavailable,
    # the model uses the hardcoded 0.04 default. growth_source must read
    # "default_fallback" — labeling it "historical_fcf_cagr" would lie
    # about the provenance of the input.
    #
    # Setup: smoothed_base_fcf must be positive (DCF actually runs),
    # but default_growth_from_history must return None (start endpoint <= 0).
    # Recipe: 5 FCF observations oldest→newest = [-1.0, 5.0, 6.0, 7.0, 8.0]
    # → median of last 3 = 7.0 (positive base_fcf)
    # → start=-1 → default_growth_from_history returns None.
    session.add(
        Profile(
            symbol="DFB", sector="Tech", beta=1.0, raw={}, known_at=datetime.now(UTC)
        )
    )
    session.add(
        IncomeStatement(
            symbol="DFB",
            period="FY",
            fiscal_date=date(2024, 12, 31),
            net_income=1e9,
            eps_diluted=5.0,
            known_at=datetime.now(UTC),
        )
    )
    for i, fcf in enumerate([-1.0, 5.0, 6.0, 7.0, 8.0]):  # oldest→newest
        session.add(
            CashFlow(
                symbol="DFB",
                period="FY",
                fiscal_date=date(2020 + i, 12, 31),
                free_cash_flow=fcf,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    out = value_company(session, "DFB", model="dcf")
    assert out["models"]["dcf"]["growth_source"] == "default_fallback"
    assumptions = out["models"]["dcf"]["core"]["assumptions"]
    assert assumptions["growth_rate"] == 0.04
