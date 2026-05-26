"""MD: news refresh + research bundler — boundary cases + DB roundtrip."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.db import Base
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.holdings import Holding
from quant_researcher.models.insider import InsiderTransaction
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.research import NewsItem, ResearchBundle
from quant_researcher.models.short_interest import ShortInterest
from quant_researcher.models.transcripts import Transcript
from quant_researcher.models.valuation import ValuationSnapshot
from quant_researcher.research.bundler import build_bundle, bundle
from quant_researcher.research.refresh import refresh_news


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


@pytest.fixture
def fmp() -> MagicMock:
    return MagicMock(spec=FMPClient)


# ----- refresh_news -------------------------------------------------------


def test_refresh_news_inserts_new(session: Session, fmp: MagicMock) -> None:
    fmp.get_news.return_value = [
        {
            "symbol": "AAPL",
            "publishedDate": "2026-05-20 10:00:00",
            "title": "Apple beats",
            "url": "https://example.com/a",
            "site": "Bloomberg",
            "text": "summary text",
        },
        {
            "symbol": "MSFT",
            "publishedDate": "2026-05-20 11:00:00",
            "title": "Microsoft launches",
            "url": "https://example.com/b",
            "site": "Reuters",
        },
    ]
    result = refresh_news(session, fmp, ["AAPL", "MSFT"])
    session.commit()
    assert result.fetched == 2
    assert result.inserted == 2
    rows = list(session.scalars(select(NewsItem).order_by(NewsItem.symbol)))
    assert len(rows) == 2
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.headline == "Apple beats"
    assert aapl.source == "Bloomberg"
    assert aapl.summary == "summary text"


def test_refresh_news_dedupes_on_rerun(session: Session, fmp: MagicMock) -> None:
    fmp.get_news.return_value = [
        {
            "symbol": "AAPL",
            "publishedDate": "2026-05-20 10:00:00",
            "title": "Same headline",
            "url": "https://example.com/a",
        }
    ]
    refresh_news(session, fmp, ["AAPL"])
    session.commit()
    result2 = refresh_news(session, fmp, ["AAPL"])
    session.commit()
    assert result2.inserted == 0
    assert result2.skipped_duplicate == 1
    assert len(list(session.scalars(select(NewsItem)))) == 1


def test_refresh_news_drops_rows_without_pk(session: Session, fmp: MagicMock) -> None:
    fmp.get_news.return_value = [
        {"symbol": "", "publishedDate": "2026-05-20", "url": "..."},  # no symbol
        {"symbol": "AAPL", "publishedDate": None, "url": "..."},  # no date
        {"symbol": "AAPL", "publishedDate": "2026-05-20", "url": ""},  # no url
        {
            "symbol": "AAPL",
            "publishedDate": "2026-05-20 10:00:00",
            "url": "https://x.com/a",
            "title": "Good one",
        },
    ]
    result = refresh_news(session, fmp, ["AAPL"])
    session.commit()
    assert result.inserted == 1


def test_refresh_news_handles_fmp_error_softly(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_news.side_effect = FMPError("HTTP 402 premium", status_code=402)
    result = refresh_news(session, fmp, ["AAPL"])
    assert result.inserted == 0
    assert len(result.failed) == 1
    assert "premium" in result.failed[0]["error"]


def test_refresh_news_empty_symbols(session: Session, fmp: MagicMock) -> None:
    result = refresh_news(session, fmp, [])
    assert result.fetched == 0
    fmp.get_news.assert_not_called()


# ----- bundler: build_bundle ---------------------------------------------


def _seed_full_robust(session: Session, sym: str = "AAPL") -> None:
    """Seed enough data so every bundle section can populate."""
    from quant_researcher.models.estimates import AnalystEstimate

    session.add(
        Profile(
            symbol=sym,
            company_name="Apple Inc.",
            sector="Technology",
            industry="Consumer Electronics",
            exchange="NASDAQ",
            beta=1.2,
            raw={"mktCap": 3e12, "companyName": "Apple Inc."},
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        DailyPrice(
            symbol=sym, trade_date=date.today() - timedelta(days=1), close=200.0
        )
    )
    session.add(
        FinancialRatios(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            pe_ratio=28.0,
            peg_ratio=2.1,
            ev_to_ebitda=22.0,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        IncomeStatement(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            revenue=400e9,
            net_income=100e9,
            eps_diluted=6.5,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        BalanceSheet(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            total_assets=400e9,
            total_equity=80e9,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        CashFlow(
            symbol=sym,
            period="FY",
            fiscal_date=date(2024, 9, 30),
            operating_cash_flow=120e9,
            free_cash_flow=100e9,
            known_at=datetime.now(UTC),
        )
    )
    # Forward estimate must be in the future relative to today.
    session.add(
        AnalystEstimate(
            symbol=sym,
            fiscal_date=date.today() + timedelta(days=180),
            period="FY",
            revenue_avg=420e9,
            eps_avg=7.5,
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        ValuationSnapshot(
            snapshot_id="snap1",
            symbol=sym,
            model_type="dcf",
            as_of=date.today(),
            fair_value_per_share=225.0,
            current_price=200.0,
            upside_pct=0.125,
        )
    )
    session.add(
        Holding(
            account_id="U1",
            symbol=sym,
            as_of_date=date.today(),
            asset_category="STK",
            quantity=100.0,
            mark_price=200.0,
            market_value=20000.0,
            avg_cost=150.0,
            side="Long",
            source="csv",
        )
    )
    session.add(
        NewsItem(
            symbol=sym,
            published_at=datetime.now(UTC) - timedelta(hours=2),
            url="https://example.com/a",
            headline="Big news",
        )
    )
    session.commit()


def test_build_bundle_aggregates_all_sections(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    payload = build_bundle(session, "AAPL")

    assert payload["symbol"] == "AAPL"
    assert payload["profile"]["sector"] == "Technology"
    assert payload["profile"]["market_cap"] == 3e12
    assert payload["latest_price"]["close"] == 200.0
    assert payload["ratios_latest_annual"]["pe_ratio"] == 28.0
    assert len(payload["income_statement_recent"]) == 1
    assert payload["income_statement_recent"][0]["revenue"] == 400e9
    assert len(payload["balance_sheet_recent"]) == 1
    assert len(payload["cash_flow_recent"]) == 1
    assert len(payload["estimates_forward"]) == 1
    assert payload["estimates_forward"][0]["eps_avg"] == 7.5
    assert len(payload["valuation_snapshots"]) == 1
    assert payload["valuation_snapshots"][0]["model_type"] == "dcf"
    assert len(payload["holdings"]) == 1
    assert payload["holdings"][0]["account_id"] == "U1"
    assert len(payload["news"]) == 1
    assert payload["news"][0]["headline"] == "Big news"


def test_build_bundle_missing_symbol_returns_skeleton(session: Session) -> None:
    payload = build_bundle(session, "GHOST")
    assert payload["symbol"] == "GHOST"
    assert payload["profile"] is None
    assert payload["latest_price"] is None
    assert payload["ratios_latest_annual"] is None
    assert payload["income_statement_recent"] == []
    assert payload["news"] == []


def test_bundle_persists_snapshot(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    bundle_id, payload = bundle(session, "AAPL")
    session.commit()
    assert bundle_id is not None
    row = session.get(ResearchBundle, bundle_id)
    assert row is not None
    assert row.symbol == "AAPL"
    assert row.payload["profile"]["sector"] == "Technology"


def test_bundle_skip_save(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    bundle_id, payload = bundle(session, "AAPL", save=False)
    assert bundle_id is None
    assert payload["symbol"] == "AAPL"
    assert session.scalars(select(ResearchBundle)).first() is None


def test_bundle_transcript_section_truncates(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    session.add(
        Transcript(
            symbol="AAPL", year=2025, quarter=2,
            call_date=date(2025, 5, 1), content="abc " * 1000,
        )
    )
    session.commit()
    _, payload = bundle(session, "AAPL", save=False)
    t = payload["transcript"]
    assert t is not None
    assert t["year"] == 2025
    assert t["quarter"] == 2
    assert t["call_date"] == "2025-05-01"
    assert t["excerpt"] is not None
    assert len(t["excerpt"]) <= 2000


def test_bundle_transcript_section_none_when_missing(session: Session) -> None:
    _seed_full_robust(session, "AAPL")  # no Transcript seeded
    _, payload = bundle(session, "AAPL", save=False)
    assert payload["transcript"] is None


def test_bundle_insider_section(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    td = date.today() - timedelta(days=12)
    fd = date.today() - timedelta(days=10)
    session.add_all([
        InsiderTransaction(
            symbol="AAPL", accession_no="a", line_no=0, filing_date=fd,
            transaction_date=td, insider="X", position="CEO",
            transaction_type="Purchase", code="P", shares=1000.0, price=50.0, value=50000.0,
        ),
        InsiderTransaction(
            symbol="AAPL", accession_no="a", line_no=1, filing_date=fd,
            transaction_date=td, insider="Y", position="CFO",
            transaction_type="Sale", code="S", shares=200.0, price=51.0, value=10200.0,
        ),
    ])
    session.commit()
    p = build_bundle(session, "AAPL")
    ins = p["insider"]
    assert ins["transactions"] == 2
    assert ins["open_market_buys"] == 1
    assert ins["open_market_sells"] == 1
    assert ins["net_open_market_value"] == pytest.approx(50000.0 - 10200.0)
    assert len(ins["recent"]) == 2


def test_bundle_insider_none_when_missing(session: Session) -> None:
    _seed_full_robust(session, "AAPL")  # no insider rows
    p = build_bundle(session, "AAPL")
    assert p["insider"] is None


def test_bundle_short_interest_section(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    session.add(
        ShortInterest(
            symbol="AAPL", settlement_date=date(2026, 4, 30), short_interest=12e6,
            previous_short_interest=10e6, change_pct=20.0, avg_daily_volume=5e6,
            days_to_cover=2.4,
        )
    )
    session.commit()
    si = build_bundle(session, "AAPL")["short_interest"]
    assert si["settlement_date"] == "2026-04-30"
    assert si["short_interest"] == 12e6
    assert si["days_to_cover"] == 2.4


def test_bundle_short_interest_none_when_missing(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    assert build_bundle(session, "AAPL")["short_interest"] is None


def _seed_two_years(session: Session, sym: str = "MSFT") -> None:
    """Two FY years with full balance-sheet fields + ratios so scores compute."""
    session.add(
        Profile(symbol=sym, sector="Technology", beta=1.1,
                raw={"marketCap": 3e12}, known_at=datetime.now(UTC))
    )
    session.add(
        DailyPrice(symbol=sym, trade_date=date.today() - timedelta(days=1), close=400.0)
    )
    # (year, ni, rev, gp, oi, eps, ta, tl, te, ltd, re, ca, cl, ocf, fcf,
    #  pe, ev_ebitda, ps, pb, fcf_yield, roic)
    rows = [
        (2023, 70e9, 210e9, 140e9, 80e9, 9.0, 380e9, 190e9, 190e9, 60e9, 120e9,
         160e9, 90e9, 85e9, 65e9, 30.0, 20.0, 11.0, 12.0, 0.03, 0.25),
        (2024, 90e9, 245e9, 170e9, 100e9, 12.0, 410e9, 180e9, 230e9, 55e9, 160e9,
         180e9, 95e9, 110e9, 95e9, 34.0, 23.0, 12.0, 14.0, 0.035, 0.30),
    ]
    for (yr, ni, rev, gp, oi, eps, ta, tl, te, ltd, re, ca, cl, ocf, fcf,
         pe, evebitda, ps, pb, fcfy, roic) in rows:
        fd = date(yr, 6, 30)
        session.add(IncomeStatement(
            symbol=sym, period="FY", fiscal_date=fd, revenue=rev, net_income=ni,
            gross_profit=gp, operating_income=oi, eps_diluted=eps,
            known_at=datetime.now(UTC)))
        session.add(BalanceSheet(
            symbol=sym, period="FY", fiscal_date=fd, total_assets=ta,
            total_liabilities=tl, total_equity=te, long_term_debt=ltd,
            retained_earnings=re, current_assets=ca, current_liabilities=cl,
            known_at=datetime.now(UTC)))
        session.add(CashFlow(
            symbol=sym, period="FY", fiscal_date=fd, operating_cash_flow=ocf,
            free_cash_flow=fcf, known_at=datetime.now(UTC)))
        session.add(FinancialRatios(
            symbol=sym, period="FY", fiscal_date=fd, pe_ratio=pe,
            ev_to_ebitda=evebitda, price_to_sales=ps, price_to_book=pb,
            fcf_yield=fcfy, return_on_invested_capital=roic,
            known_at=datetime.now(UTC)))
    session.commit()


def test_bundle_scores_section_computes(session: Session) -> None:
    _seed_two_years(session, "MSFT")
    p = build_bundle(session, "MSFT")
    sc = p["scores"]
    assert sc["fiscal_year"] == 2024
    assert sc["prior_fiscal_year"] == 2023
    assert sc["piotroski_f"]["score"] == 9  # all nine legs improve YoY in the seed
    assert sc["piotroski_f"]["max_possible"] == 9
    assert sc["altman_z"]["zone"] in {"safe", "grey", "distress"}
    # ROIC now surfaced in the latest-annual ratios section
    assert p["ratios_latest_annual"]["roic"] == pytest.approx(0.30)


def test_bundle_quality_and_history(session: Session) -> None:
    _seed_two_years(session, "MSFT")
    p = build_bundle(session, "MSFT")
    q = p["quality"]
    assert q["fcf_conversion"] == pytest.approx(95e9 / 90e9)
    assert q["roic"] == pytest.approx(0.30)
    assert q["trends"]["revenue"]["direction"] == "up"
    rh = p["ratio_history"]
    assert len(rh["multiples"]["pe_ratio"]) == 2
    assert rh["multiples"]["fiscal_dates"][0] == "2023-06-30"  # ascending order


def test_bundle_scores_none_when_no_financials(session: Session) -> None:
    session.add(Profile(symbol="GHOST2", raw={}, known_at=datetime.now(UTC)))
    session.commit()
    p = build_bundle(session, "GHOST2")
    assert p["scores"] is None
    assert p["quality"] is None
    assert p["ratio_history"] is None


def test_bundle_general_template_emits_template_keys(session: Session) -> None:
    # Existing general path (Tech): the historical Piotroski / Altman /
    # ROIC-WACC computation is preserved. Issue #37 only adds the
    # `template: "general"` discriminator + `stock_type: "general"` on
    # profile, alongside the existing keys.
    _seed_two_years(session, "MSFT")
    p = build_bundle(session, "MSFT")
    assert p["profile"]["stock_type"] == "general"
    assert p["scores"]["template"] == "general"
    assert p["scores"]["piotroski_f"]["score"] == 9  # unchanged
    assert p["quality"]["template"] == "general"
    assert "roic_wacc_spread" in p["quality"]      # unchanged
    assert "net_interest_margin" not in p["quality"]


# ----- bank template (issue #37 phase 1) ---------------------------------


def _seed_bank(session: Session, sym: str = "JPM") -> None:
    """Two FY years of bank financials. Sector / industry mark it as a bank.

    The raw JSON carries the bank-specific FMP fields (`netInterestIncome`
    / `nonInterestIncome` / `nonInterestExpense`) that aren't typed
    columns but are present in real FMP payloads (confirmed for GS).
    """
    session.add(
        Profile(
            symbol=sym,
            sector="Financial Services",
            industry="Banks—Diversified",
            beta=1.2,
            raw={"marketCap": 600e9},
            known_at=datetime.now(UTC),
        )
    )
    session.add(
        DailyPrice(symbol=sym, trade_date=date.today() - timedelta(days=1), close=200.0)
    )
    rows = [
        # (year, net_income, total_assets, total_equity,
        #  NII, non-int income, operating-expenses, interestIncome, interestExpense)
        # FMP's bank payload exposes `netInterestIncome` + `interestIncome`
        # + `interestExpense` + `operatingExpenses`; `revenue` is bank-gross
        # (II + non-II). Non-interest income is derived by the bundler as
        # revenue − interestIncome; net revenue as revenue − interestExpense.
        (2023, 48e9, 3.8e12, 320e9, 90e9, 70e9, 88e9, 180e9, 90e9),
        (2024, 58e9, 4.0e12, 350e9, 95e9, 75e9, 90e9, 200e9, 105e9),
    ]
    for yr, ni, ta, te, nii, non_int_inc, op_exp, int_inc, int_exp in rows:
        fd = date(yr, 12, 31)
        # gross revenue = interestIncome + nonInterestIncome
        gross_rev = int_inc + non_int_inc
        session.add(
            IncomeStatement(
                symbol=sym,
                period="FY",
                fiscal_date=fd,
                net_income=ni,
                revenue=gross_rev,
                known_at=datetime.now(UTC),
                raw={
                    "netInterestIncome": nii,
                    "interestIncome": int_inc,
                    "interestExpense": int_exp,
                    "operatingExpenses": op_exp,
                },
            )
        )
        session.add(
            BalanceSheet(
                symbol=sym,
                period="FY",
                fiscal_date=fd,
                total_assets=ta,
                total_equity=te,
                total_liabilities=ta - te,
                known_at=datetime.now(UTC),
            )
        )
    session.commit()


def test_bundle_bank_profile_classified_correctly(session: Session) -> None:
    _seed_bank(session, "JPM")
    p = build_bundle(session, "JPM")
    assert p["profile"]["stock_type"] == "bank"
    assert p["profile"]["sector"] == "Financial Services"
    assert p["profile"]["industry"] == "Banks—Diversified"


def test_bundle_bank_scores_section_marks_piotroski_altman_na(
    session: Session,
) -> None:
    _seed_bank(session, "JPM")
    p = build_bundle(session, "JPM")
    sc = p["scores"]
    assert sc["template"] == "bank"
    assert "piotroski_f" in sc["not_applicable"]
    assert "altman_z" in sc["not_applicable"]
    assert "non-financial balance sheet" in sc["not_applicable_reason"]
    # No general-template keys leak through.
    assert "piotroski_f" not in sc or sc.get("piotroski_f") is None
    assert "altman_z" not in sc or sc.get("altman_z") is None


def test_bundle_bank_income_statement_carries_revenue_net(
    session: Session,
) -> None:
    # Issue #36: for banks, `income_statement_recent[*]` must additively
    # carry `revenue_net` (revenue − interestExpense) so downstream
    # consumers don't accidentally headline FMP's gross revenue.
    _seed_bank(session, "JPM")
    p = build_bundle(session, "JPM")
    inc = p["income_statement_recent"][0]  # FY2024 most-recent
    # Gross revenue stays — it's what FMP reports.
    assert inc["revenue"] == 200e9 + 75e9  # interestIncome + non-int income
    # Net revenue = revenue − interestExpense = 275 − 105 = 170.
    assert inc["revenue_net"] == 275e9 - 105e9


def test_bundle_general_income_statement_omits_revenue_net(
    session: Session,
) -> None:
    # AAPL (general): `revenue_net` would be redundant (gross == net) and
    # must NOT be emitted — keeps the existing payload byte-for-byte.
    _seed_two_years(session, "MSFT")
    p = build_bundle(session, "MSFT")
    inc = p["income_statement_recent"][0]
    assert "revenue_net" not in inc


def test_bundle_bank_quality_section_emits_bank_metrics(session: Session) -> None:
    _seed_bank(session, "JPM")
    p = build_bundle(session, "JPM")
    q = p["quality"]
    assert q["template"] == "bank"
    # FY24 values from the seed:
    #   ROA = 58e9 / 4.0e12 = 0.0145
    #   ROE = 58e9 / 350e9  ≈ 0.1657
    #   NIM = 95e9 / ((4.0e12 + 3.8e12) / 2) = 95 / 3900 ≈ 0.02436
    #   Efficiency = 90 / (95 + 75) = 90 / 170 ≈ 0.5294
    #   Equity/Assets = 350e9 / 4.0e12 = 0.0875
    assert q["roa"] == pytest.approx(58e9 / 4.0e12)
    assert q["roe"] == pytest.approx(58e9 / 350e9)
    assert q["net_interest_margin"] == pytest.approx(95e9 / ((4.0e12 + 3.8e12) / 2))
    assert q["efficiency_ratio"] == pytest.approx(90.0 / (95.0 + 75.0))
    assert q["equity_to_assets"] == pytest.approx(350e9 / 4.0e12)
    # Revenue trend still meaningful for banks (total revenue is well-defined).
    assert q["trends"]["revenue"]["direction"] == "up"
    # Honest about what's missing.
    assert "tier_1_capital_ratio" in q["missing_fields"]
    assert "npl_ratio" in q["missing_fields"]
    # And about what's not applicable.
    assert "roic_wacc_spread" in q["not_applicable"]
    assert "fcf_conversion" in q["not_applicable"]


def test_bundle_holdings_picks_latest_per_account(session: Session) -> None:
    """Multiple snapshots per (account, symbol) → bundle takes the most recent per account."""
    sym = "AAPL"
    session.add(
        Profile(symbol=sym, raw={}, known_at=datetime.now(UTC))
    )
    today = date.today()
    session.add(
        Holding(
            account_id="U1",
            symbol=sym,
            as_of_date=today - timedelta(days=2),
            asset_category="STK",
            quantity=50.0,
            source="csv",
        )
    )
    session.add(
        Holding(
            account_id="U1",
            symbol=sym,
            as_of_date=today,
            asset_category="STK",
            quantity=100.0,
            source="csv",
        )
    )
    session.commit()
    payload = build_bundle(session, sym)
    assert len(payload["holdings"]) == 1
    assert payload["holdings"][0]["quantity"] == 100.0
