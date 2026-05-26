"""Valuation data accessors — coverage on the non-trivial ones."""

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
from quant_researcher.valuation.helpers import (
    earnings_growth_rate,
    forward_eps_growth_rate,
    historical_fcf,
    latest_close,
    latest_ebitda,
    latest_market_cap,
    net_debt,
    sector_peer_median,
    shares_outstanding,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


# ----- historical_fcf -----------------------------------------------------


def test_historical_fcf_sorted_oldest_to_newest(session: Session) -> None:
    for i, fcf in enumerate([10.0, 15.0, 20.0]):  # 2022, 2023, 2024
        session.add(
            CashFlow(
                symbol="AAPL",
                period="FY",
                fiscal_date=date(2022 + i, 9, 30),
                free_cash_flow=fcf,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    assert historical_fcf(session, "AAPL", n=5) == [10.0, 15.0, 20.0]


def test_historical_fcf_respects_limit(session: Session) -> None:
    for i in range(6):
        session.add(
            CashFlow(
                symbol="AAPL",
                period="FY",
                fiscal_date=date(2019 + i, 9, 30),
                free_cash_flow=float(i),
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    out = historical_fcf(session, "AAPL", n=3)
    assert len(out) == 3
    # Latest 3 (oldest→newest): 3.0, 4.0, 5.0
    assert out == [3.0, 4.0, 5.0]


def test_historical_fcf_skips_quarter_rows(session: Session) -> None:
    session.add(
        CashFlow(
            symbol="AAPL",
            period="Q1",
            fiscal_date=date(2024, 3, 31),
            free_cash_flow=99.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        CashFlow(
            symbol="AAPL",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            free_cash_flow=50.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert historical_fcf(session, "AAPL") == [50.0]


# ----- net_debt -----------------------------------------------------------


def test_net_debt_positive_when_debt_exceeds_cash(session: Session) -> None:
    session.add(
        BalanceSheet(
            symbol="AAPL",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            short_term_debt=10.0,
            long_term_debt=90.0,
            cash_and_equivalents=30.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert net_debt(session, "AAPL") == 70.0  # 100 - 30


def test_net_debt_negative_when_cash_rich(session: Session) -> None:
    session.add(
        BalanceSheet(
            symbol="AAPL",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            short_term_debt=0.0,
            long_term_debt=20.0,
            cash_and_equivalents=80.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert net_debt(session, "AAPL") == -60.0


def test_net_debt_none_when_no_row(session: Session) -> None:
    assert net_debt(session, "GHOST") is None


# ----- shares_outstanding -------------------------------------------------


def test_shares_outstanding_from_net_income_div_eps(session: Session) -> None:
    session.add(
        IncomeStatement(
            symbol="AAPL",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            net_income=100e9,
            eps_diluted=6.5,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert shares_outstanding(session, "AAPL") == pytest.approx(100e9 / 6.5)


def test_shares_outstanding_none_when_missing(session: Session) -> None:
    assert shares_outstanding(session, "GHOST") is None


def test_shares_outstanding_none_when_eps_zero(session: Session) -> None:
    session.add(
        IncomeStatement(
            symbol="X",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            net_income=100.0,
            eps_diluted=0.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert shares_outstanding(session, "X") is None


def test_shares_outstanding_prefers_profile_market_cap_over_eps(
    session: Session,
) -> None:
    # When `/profile` carries marketCap + price, that ratio is the freshest
    # share count we have (refreshed nightly with the price). Even if the
    # latest FY income statement implies a different share count (typical
    # for buyback-heavy names where the fiscal-year average EPS lags the
    # current share count), the profile-derived value wins.
    session.add(
        Profile(
            symbol="BUYBACK",
            known_at=datetime.now(UTC),
            raw={"symbol": "BUYBACK", "marketCap": 294e9, "price": 1000.0},
        )
    )
    session.add(
        IncomeStatement(
            symbol="BUYBACK",
            period="FY",
            fiscal_date=date(2025, 12, 31),
            net_income=17.176e9,
            eps_diluted=51.32,  # → EPS path would give 334.7M shares (stale)
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    # profile path: 294e9 / 1000 = 294M (current, post-buyback)
    assert shares_outstanding(session, "BUYBACK") == pytest.approx(294e6)


def test_shares_outstanding_falls_back_to_eps_when_profile_missing(
    session: Session,
) -> None:
    # No Profile row → fall back to net_income / eps_diluted (legacy path).
    session.add(
        IncomeStatement(
            symbol="LEGACY",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            net_income=100e9,
            eps_diluted=6.5,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert shares_outstanding(session, "LEGACY") == pytest.approx(100e9 / 6.5)


def test_shares_outstanding_falls_back_when_profile_lacks_price(
    session: Session,
) -> None:
    # Profile present but missing price → can't derive ratio, fall back.
    session.add(
        Profile(
            symbol="NOPRICE",
            known_at=datetime.now(UTC),
            raw={"symbol": "NOPRICE", "marketCap": 100e9},  # price missing
        )
    )
    session.add(
        IncomeStatement(
            symbol="NOPRICE",
            period="FY",
            fiscal_date=date(2024, 12, 31),
            net_income=10e9,
            eps_diluted=5.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert shares_outstanding(session, "NOPRICE") == pytest.approx(10e9 / 5.0)


def test_shares_outstanding_falls_back_when_profile_mcap_non_positive(
    session: Session,
) -> None:
    # Corrupted / just-listed-pre-trading profile shows mcap = 0. The primary
    # path must reject this and fall back to EPS — propagating zero shares
    # would corrupt every per-share denominator downstream.
    session.add(
        Profile(
            symbol="ZEROMCAP",
            known_at=datetime.now(UTC),
            raw={"symbol": "ZEROMCAP", "marketCap": 0, "price": 50.0},
        )
    )
    session.add(
        IncomeStatement(
            symbol="ZEROMCAP",
            period="FY",
            fiscal_date=date(2024, 12, 31),
            net_income=200e6,
            eps_diluted=2.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    # Falls back to EPS path: 200M / 2.0 = 100M shares (not 0 from mcap/price).
    assert shares_outstanding(session, "ZEROMCAP") == pytest.approx(100e6)


# ----- latest_close / market_cap -----------------------------------------


def test_latest_close_picks_newest_bar(session: Session) -> None:
    today = date.today()
    session.add(DailyPrice(symbol="AAPL", trade_date=today - timedelta(days=1), close=100.0))
    session.add(DailyPrice(symbol="AAPL", trade_date=today, close=105.0))
    session.commit()
    assert latest_close(session, "AAPL") == 105.0


def test_latest_market_cap_from_profile_raw(session: Session) -> None:
    session.add(
        Profile(
            symbol="AAPL",
            raw={"mktCap": 3.5e12, "companyName": "Apple"},
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert latest_market_cap(session, "AAPL") == 3.5e12


def test_latest_market_cap_handles_alt_key(session: Session) -> None:
    session.add(
        Profile(
            symbol="AAPL", raw={"marketCap": 2e12}, known_at=datetime.now(UTC)
        )
    )
    session.commit()
    assert latest_market_cap(session, "AAPL") == 2e12


# ----- sector_peer_median ------------------------------------------------


def test_sector_peer_median_computes(session: Session) -> None:
    for sym, sector, pe in (
        ("A", "Tech", 20.0),
        ("B", "Tech", 30.0),
        ("C", "Tech", 25.0),
        ("D", "Health", 15.0),
    ):
        session.add(
            Profile(symbol=sym, sector=sector, raw={}, known_at=datetime.now(UTC))
        )
        session.add(
            FinancialRatios(
                symbol=sym,
                period="FY",
                fiscal_date=date(2024, 9, 30),
                pe_ratio=pe,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    # Tech: 20, 25, 30 → median 25
    assert sector_peer_median(session, "Tech", "pe_ratio") == 25.0


def test_sector_peer_median_none_for_empty_sector(session: Session) -> None:
    assert sector_peer_median(session, "GhostSector", "pe_ratio") is None


# ----- earnings_growth_rate ----------------------------------------------


def test_earnings_growth_rate_cagr(session: Session) -> None:
    # net_income doubles over 4 years → CAGR = 2^(1/4) - 1 ≈ 0.189
    for i, ni in enumerate([100.0, 120.0, 145.0, 175.0, 200.0]):
        session.add(
            IncomeStatement(
                symbol="A",
                period="FY",
                fiscal_date=date(2020 + i, 9, 30),
                net_income=ni,
                eps_diluted=1.0,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    g = earnings_growth_rate(session, "A", n=5)
    assert g == pytest.approx((200 / 100) ** (1 / 4) - 1)


def test_earnings_growth_rate_none_when_negative_start(session: Session) -> None:
    for i, ni in enumerate([-10.0, 5.0]):
        session.add(
            IncomeStatement(
                symbol="X",
                period="FY",
                fiscal_date=date(2023 + i, 9, 30),
                net_income=ni,
                eps_diluted=1.0,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    assert earnings_growth_rate(session, "X") is None


def test_earnings_growth_rate_none_when_negative_newest(session: Session) -> None:
    # Latest year a loss (newest < 0), 3+ points: (newest/oldest)**(1/years)
    # would be a COMPLEX number (silent) and crash callers. Must return None.
    for i, ni in enumerate([100.0, 150.0, -20.0]):  # newest (2024) negative
        session.add(
            IncomeStatement(
                symbol="Z",
                period="FY",
                fiscal_date=date(2022 + i, 9, 30),
                net_income=ni,
                eps_diluted=1.0,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    assert earnings_growth_rate(session, "Z", n=5) is None


# ----- latest_ebitda ------------------------------------------------------


# ----- forward_eps_growth_rate -------------------------------------------


def test_forward_eps_growth_rate_basic(session: Session) -> None:
    # Forward EPS doubles over 2 forward FYs → endpoint CAGR over 2 years
    # equals 2 ** (1/2) - 1 ≈ 0.414.
    today = date.today()
    for i, eps in enumerate([10.0, 14.142, 20.0]):
        session.add(
            AnalystEstimate(
                symbol="FWD",
                period="FY",
                fiscal_date=today + timedelta(days=365 * (i + 1)),
                eps_avg=eps,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()
    g = forward_eps_growth_rate(session, "FWD", n_periods=3)
    assert g == pytest.approx((20.0 / 10.0) ** (1 / 2) - 1, rel=1e-3)


def test_forward_eps_growth_rate_skips_past_fiscal_dates(session: Session) -> None:
    # A past-dated estimate (FY already reported) must be excluded — using it
    # would mix actuals-revisions with true forward consensus.
    today = date.today()
    session.add(
        AnalystEstimate(
            symbol="MIX",
            period="FY",
            fiscal_date=today - timedelta(days=180),
            eps_avg=1.0,  # stale → must be filtered
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        AnalystEstimate(
            symbol="MIX",
            period="FY",
            fiscal_date=today + timedelta(days=180),
            eps_avg=10.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        AnalystEstimate(
            symbol="MIX",
            period="FY",
            fiscal_date=today + timedelta(days=545),
            eps_avg=12.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    g = forward_eps_growth_rate(session, "MIX", n_periods=3)
    # If past row leaked in we'd compute 12/1 — instead should be 12/10.
    assert g == pytest.approx(12.0 / 10.0 - 1, rel=1e-3)


def test_forward_eps_growth_rate_none_when_fewer_than_two(session: Session) -> None:
    session.add(
        AnalystEstimate(
            symbol="ONE",
            period="FY",
            fiscal_date=date.today() + timedelta(days=180),
            eps_avg=5.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert forward_eps_growth_rate(session, "ONE") is None


def test_forward_eps_growth_rate_none_on_non_positive_endpoint(
    session: Session,
) -> None:
    # GS-style cyclical scenario where analysts model a near-term loss.
    today = date.today()
    session.add(
        AnalystEstimate(
            symbol="NEG",
            period="FY",
            fiscal_date=today + timedelta(days=180),
            eps_avg=-0.5,  # forecast loss next FY → start non-positive
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        AnalystEstimate(
            symbol="NEG",
            period="FY",
            fiscal_date=today + timedelta(days=545),
            eps_avg=2.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    # CAGR is undefined when start is non-positive — fall back to historical.
    assert forward_eps_growth_rate(session, "NEG") is None


def test_forward_eps_growth_rate_uses_actual_year_span_on_gaps(
    session: Session,
) -> None:
    # Far-out analyst coverage drops off: FY+1 and FY+3 are present, FY+2 is
    # gone. The CAGR must be computed over the actual 2-year span, NOT over
    # `len(series) - 1 = 1` — that would double the implied growth.
    today = date.today()
    session.add(
        AnalystEstimate(
            symbol="GAP",
            period="FY",
            fiscal_date=today + timedelta(days=365),
            eps_avg=10.0,
            known_at=datetime.now(UTC),
        )
    )
    # FY+2 missing on purpose.
    session.add(
        AnalystEstimate(
            symbol="GAP",
            period="FY",
            fiscal_date=today + timedelta(days=365 * 3),
            eps_avg=14.4,  # 20% CAGR over 2 years ⇒ 10 × 1.2^2 = 14.4
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    g = forward_eps_growth_rate(session, "GAP", n_periods=3)
    # Length-based years=1 would give 44% (14.4/10 - 1); the correct
    # answer is 20% over the real 2-year span.
    assert g == pytest.approx(0.20, rel=1e-2)


# ----- latest_ebitda ------------------------------------------------------


def test_latest_ebitda_approximates_with_capex(session: Session) -> None:
    session.add(
        IncomeStatement(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            operating_income=100.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        CashFlow(
            symbol="A",
            period="FY",
            fiscal_date=date(2024, 9, 30),
            capital_expenditure=-15.0,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()
    # operating_income + abs(capex) = 100 + 15 = 115
    assert latest_ebitda(session, "A") == 115.0
