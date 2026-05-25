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
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.research import NewsItem, ResearchBundle
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


def test_bundle_transcript_excerpt_truncates(session: Session) -> None:
    _seed_full_robust(session, "AAPL")
    long_transcript = "abc " * 1000
    _, payload = bundle(session, "AAPL", save=False, transcript_excerpt=long_transcript)
    assert payload["transcript_excerpt"] is not None
    assert len(payload["transcript_excerpt"]) <= 2000


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
