"""PEG + relative multiples — pure functions + seeded DB."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.valuation.multiples import (
    ev_ebitda_implied_price,
    pe_implied_price,
    value_via_multiples,
)
from quant_researcher.valuation.peg import peg_value, value_via_peg


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


# ----- peg_value pure function --------------------------------------------


def test_peg_value_basic() -> None:
    # PE=20, growth=15% → PEG = 20/15 = 1.333; fair_pe = 15.
    out = peg_value(pe=20.0, growth_rate=0.15, eps=5.0)
    assert out["peg_ratio"] == pytest.approx(20 / 15)
    assert out["fair_pe"] == pytest.approx(15)
    assert out["fair_value_per_share"] == pytest.approx(15 * 5.0)


def test_peg_value_undervalued() -> None:
    out = peg_value(pe=10.0, growth_rate=0.15, eps=2.0)
    assert out["peg_ratio"] < 1.0
    assert out["interpretation"] in {"undervalued", "deeply_undervalued"}


def test_peg_value_overvalued() -> None:
    out = peg_value(pe=50.0, growth_rate=0.05, eps=1.0)
    assert out["peg_ratio"] > 1.5
    assert out["interpretation"] == "overvalued"


def test_peg_value_missing_inputs() -> None:
    assert peg_value(None, 0.15)["peg_ratio"] is None
    assert peg_value(20.0, None)["peg_ratio"] is None
    assert peg_value(20.0, 0.0)["peg_ratio"] is None
    assert peg_value(20.0, -0.05)["peg_ratio"] is None


# ----- value_via_peg end-to-end -------------------------------------------


def test_value_via_peg_end_to_end(session: Session) -> None:
    # No forward estimates seeded → falls back to historical CAGR path.
    # Seed: 5y of net_income with steady 15% CAGR.
    for i in range(5):
        session.add(
            IncomeStatement(
                symbol="A",
                period="FY",
                fiscal_date=date(2020 + i, 9, 30),
                net_income=100.0 * (1.15**i),
                eps_diluted=5.0,
                known_at=datetime.now(UTC),
            )
        )
    session.add(
        FinancialRatios(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=18.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="A", trade_date=date(2024, 9, 30), close=90.0))
    session.commit()

    out = value_via_peg(session, "A")
    # growth ≈ 15% → fair_pe ≈ 15 → fair_price ≈ 15 * 5 = 75
    assert out["fair_value_per_share"] == pytest.approx(15.0 * 5.0, rel=1e-3)
    assert out["current_price"] == 90.0
    # Upside negative (overpriced vs Lynch fair).
    assert out["upside_pct"] < 0
    # Fallback path was used.
    assert out["growth_source"] == "historical_cagr"


def test_value_via_peg_prefers_forward_consensus_over_historical(
    session: Session,
) -> None:
    # Seed historical growth at ~15% AND forward consensus at ~25%.
    # Forward should win → fair_pe = 25, fair_price = 25 * 4 = $100.
    for i in range(5):
        session.add(
            IncomeStatement(
                symbol="DUAL",
                period="FY",
                fiscal_date=date(2020 + i, 9, 30),
                net_income=100.0 * (1.15**i),
                eps_diluted=4.0,
                known_at=datetime.now(UTC),
            )
        )
    # Forward FY+1 EPS $5.0 → FY+2 $6.25 → FY+3 $7.8125 (25% per year).
    today = date.today()
    for i, eps in enumerate([5.0, 6.25, 7.8125]):
        session.add(
            AnalystEstimate(
                symbol="DUAL",
                period="FY",
                fiscal_date=today + timedelta(days=365 * (i + 1)),
                eps_avg=eps,
                known_at=datetime.now(UTC),
            )
        )
    session.add(
        FinancialRatios(
            symbol="DUAL",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=20.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="DUAL", trade_date=date(2024, 9, 30), close=80.0))
    session.commit()

    out = value_via_peg(session, "DUAL")
    assert out["growth_source"] == "forward_consensus"
    # Forward growth ≈ 25% → fair_pe ≈ 25 → fair_price ≈ 25 * 4 = 100
    # (NOT 15 * 4 = 60, which is what the historical path would give.)
    assert out["fair_value_per_share"] == pytest.approx(25.0 * 4.0, rel=1e-3)


def test_value_via_peg_growth_source_none_when_no_growth_available(
    session: Session,
) -> None:
    # PE ratio present but neither forward estimates nor enough income history.
    session.add(
        FinancialRatios(
            symbol="EMPTY",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=15.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="EMPTY", trade_date=date(2024, 9, 30), close=50.0))
    session.commit()
    out = value_via_peg(session, "EMPTY")
    assert out["growth_source"] is None
    assert out["fair_value_per_share"] is None


# ----- multiples helpers --------------------------------------------------


def _seed_peer(
    session: Session, sym: str, sector: str, pe: float, ev_eb: float, ps: float
) -> None:
    session.add(
        Profile(symbol=sym, sector=sector, raw={}, known_at=datetime.now(UTC))
    )
    session.add(
        FinancialRatios(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=pe,
            ev_to_ebitda=ev_eb,
            price_to_sales=ps,
            known_at=datetime.now(UTC),
        )
    )


def test_pe_implied_price_uses_peer_median(session: Session) -> None:
    _seed_peer(session, "A", "Tech", pe=20.0, ev_eb=10, ps=4)
    _seed_peer(session, "B", "Tech", pe=30.0, ev_eb=12, ps=5)
    _seed_peer(session, "C", "Tech", pe=25.0, ev_eb=11, ps=6)
    session.add(
        IncomeStatement(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            eps_diluted=2.0,
            net_income=200.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()

    # Median PE = 25; implied = 25 * 2.0 = 50
    out = pe_implied_price(session, "A", "Tech")
    assert out["peer_median_pe"] == 25.0
    assert out["implied_price"] == 50.0


def test_value_via_multiples_aggregates(session: Session) -> None:
    # Three tech peers; target is "A".
    _seed_peer(session, "A", "Tech", pe=20.0, ev_eb=10, ps=4)
    _seed_peer(session, "B", "Tech", pe=30.0, ev_eb=12, ps=5)
    _seed_peer(session, "C", "Tech", pe=25.0, ev_eb=11, ps=6)
    # Financials needed for ev_ebitda + ev_revenue.
    session.add(
        IncomeStatement(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            net_income=200.0,
            eps_diluted=2.0,
            operating_income=300.0,
            revenue=1000.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        CashFlow(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            capital_expenditure=-50.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        BalanceSheet(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            short_term_debt=10.0,
            long_term_debt=40.0,
            cash_and_equivalents=20.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="A", trade_date=date(2024, 9, 30), close=80.0))
    session.commit()

    out = value_via_multiples(session, "A")
    assert out["sector"] == "Tech"
    assert out["models"]["pe"]["implied_price"] == 50.0  # median 25 × EPS 2
    # ev_ebitda implied price: ebitda = op_income + abs(capex) = 350;
    # EV = 11 * 350 = 3850; equity = 3850 - 30 = 3820;
    # shares = 100; per share = 38.2
    assert out["models"]["ev_ebitda"]["implied_price"] == pytest.approx(38.2, rel=1e-2)
    # ev_revenue implied price: peer ps median 5 × revenue 1000 = 5000 / 100 shares = 50
    assert out["models"]["ev_revenue"]["implied_price"] == pytest.approx(50.0)
    # Aggregate average ≈ (50 + 38.2 + 50) / 3 ≈ 46.07
    assert out["fair_value_per_share"] == pytest.approx((50 + 38.2 + 50) / 3, rel=1e-2)
    assert out["current_price"] == 80.0


def test_value_via_multiples_missing_sector(session: Session) -> None:
    out = value_via_multiples(session, "GHOST")
    assert out["sector"] is None
    assert out["fair_value_per_share"] is None


# ----- sector gate + defensive blend (issue #34) -------------------------


def test_ev_ebitda_skipped_for_financial_services(session: Session) -> None:
    # For a bank, EV − net_debt blows up because "debt" includes deposits.
    # The model must short-circuit before computing and surface a note.
    out = ev_ebitda_implied_price(session, "GS", "Financial Services")
    assert out["implied_price"] is None
    assert out["peer_median_ev_ebitda"] is None
    assert out["ebitda"] is None
    assert "EV/EBITDA n/a for Financial Services" in out["note"]


def test_ev_ebitda_skipped_for_real_estate(session: Session) -> None:
    # REITs are valued on FFO / P-TBV, not EV/EBITDA. Same gate applies.
    out = ev_ebitda_implied_price(session, "PLD", "Real Estate")
    assert out["implied_price"] is None
    assert "EV/EBITDA n/a for Real Estate" in out["note"]


def test_value_via_multiples_skips_evebitda_for_banks_in_blend(
    session: Session,
) -> None:
    # Set up a Financial Services target + 3 peers so PE and EV/Revenue can
    # still compute, and assert the blended fair value is the average of
    # exactly those two (EV/EBITDA contributes nothing because of the gate).
    peers = (("GS", None, None), ("MS", 14.0, 2.0), ("JPM", 16.0, 2.5), ("BAC", 12.0, 1.8))
    for sym, pe, ps in peers:
        session.add(
            Profile(
                symbol=sym,
                sector="Financial Services",
                raw={},
                known_at=datetime.now(UTC),
            )
        )
        if pe is not None:
            session.add(
                FinancialRatios(
                    symbol=sym,
                    period="FY",
                    fiscal_date=date(2024, 12, 31),
                    pe_ratio=pe,
                    ev_to_ebitda=11.0,
                    price_to_sales=ps,
                    known_at=datetime.now(UTC),
                )
            )
    # Target financials (GS): EPS and revenue so PE / EV-Rev populate.
    session.add(
        IncomeStatement(
            symbol="GS",
            period="FY",
            fiscal_date=date(2024, 12, 31),
            net_income=15e9,
            eps_diluted=40.0,
            operating_income=20e9,
            revenue=125e9,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="GS", trade_date=date(2024, 12, 31), close=500.0))
    session.commit()

    out = value_via_multiples(session, "GS")
    assert out["sector"] == "Financial Services"
    # ev_ebitda component skipped with note, doesn't poison the blend.
    ev_eb = out["models"]["ev_ebitda"]
    assert ev_eb["implied_price"] is None
    assert "EV/EBITDA n/a" in ev_eb.get("note", "")
    # Blended fair value = mean(PE implied, EV/Revenue implied) only.
    pe_implied = out["models"]["pe"]["implied_price"]
    ev_rev_implied = out["models"]["ev_revenue"]["implied_price"]
    assert pe_implied is not None
    assert ev_rev_implied is not None
    expected = (pe_implied + ev_rev_implied) / 2
    assert out["fair_value_per_share"] == pytest.approx(expected)


def test_value_via_multiples_blend_excludes_non_positive(session: Session) -> None:
    # Belt-and-suspenders: even if some future bug lets a component return a
    # zero or negative implied price, the cross-component average must not be
    # poisoned. Simulate by seeding peers with a P/S so low that EV/Revenue
    # implies a per-share value of ~0.
    _seed_peer(session, "A", "Junk", pe=10.0, ev_eb=5, ps=4)
    _seed_peer(session, "B", "Junk", pe=12.0, ev_eb=6, ps=5)
    _seed_peer(session, "C", "Junk", pe=14.0, ev_eb=7, ps=6)
    # Target with a tiny revenue and a massive share count → EV/Rev implies ~0
    session.add(
        IncomeStatement(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            net_income=200.0,
            eps_diluted=2.0,
            operating_income=300.0,
            revenue=0.001,  # tiny → EV/Rev implied per-share rounds to ~0
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        CashFlow(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            capital_expenditure=-50.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        BalanceSheet(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            short_term_debt=0.0,
            long_term_debt=0.0,
            cash_and_equivalents=0.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(DailyPrice(symbol="A", trade_date=date(2024, 9, 30), close=20.0))
    session.commit()

    out = value_via_multiples(session, "A")
    # All three components compute; PE & EV/EBITDA come back positive,
    # EV/Revenue rounds to ~0 (≤ 0 with the tiny revenue path).
    pe = out["models"]["pe"]["implied_price"]
    ev_eb = out["models"]["ev_ebitda"]["implied_price"]
    ev_rev = out["models"]["ev_revenue"]["implied_price"]
    assert pe is not None and pe > 0
    assert ev_eb is not None and ev_eb > 0
    # EV/Rev should be effectively zero given the seeding — that's the case
    # we want the blend to drop. Whether it's exactly 0.0 or a tiny float
    # depends on shares, but it's <<< pe/ev_eb scale.
    assert ev_rev is not None  # computed
    # Blended value = mean of POSITIVE components only.
    positive = [v for v in (pe, ev_eb, ev_rev) if v > 0]
    expected = sum(positive) / len(positive)
    assert out["fair_value_per_share"] == pytest.approx(expected)
