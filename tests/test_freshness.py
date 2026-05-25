"""freshness.py — threshold edges, missing-vs-stale split, scope-specific rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.data.freshness import (
    SCOPE_THRESHOLDS,
    check_freshness,
    stale_symbols,
)
from quant_researcher.db import Base
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.insider import InsiderTransaction
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.transcripts import Transcript


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


# Fixed clock — Tuesday 2024-06-04 12:00 UTC. Day of week chosen so that
# `now - 3 days` is Saturday and `now - 4 days` is Friday, anchoring the
# quote calendar-day tests below.
NOW = datetime(2024, 6, 4, 12, 0, tzinfo=UTC)


# ----- profile threshold ---------------------------------------------------


def test_profile_fresh_when_known_at_within_30d(session: Session) -> None:
    session.add(
        Profile(symbol="AAPL", known_at=NOW - timedelta(days=29), raw={})
    )
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("profile",), now=NOW)
    sf = report.scopes["profile"]
    assert sf.fresh == ["AAPL"]
    assert sf.stale == []
    assert sf.missing == []


def test_profile_stale_when_known_at_past_30d(session: Session) -> None:
    session.add(
        Profile(symbol="AAPL", known_at=NOW - timedelta(days=31), raw={})
    )
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("profile",), now=NOW)
    sf = report.scopes["profile"]
    assert sf.fresh == []
    assert sf.stale == ["AAPL"]
    assert sf.missing == []


def test_missing_distinguished_from_stale(session: Session) -> None:
    # Universe = [A, B]. DB only has A's profile, and it's past threshold.
    session.add(
        Profile(symbol="AAPL", known_at=NOW - timedelta(days=40), raw={})
    )
    session.commit()

    report = check_freshness(session, ["AAPL", "MSFT"], scopes=("profile",), now=NOW)
    sf = report.scopes["profile"]
    assert sf.stale == ["AAPL"]
    assert sf.missing == ["MSFT"]
    assert sf.needs_refresh == ["AAPL", "MSFT"]


# ----- quote (calendar-day) ------------------------------------------------


def test_quote_monday_with_friday_bar_is_fresh(session: Session) -> None:
    # `now = Monday 2024-06-03`, latest bar = Friday 2024-05-31. Diff = 3 calendar days.
    monday_now = datetime(2024, 6, 3, 9, 0, tzinfo=UTC)
    session.add(DailyPrice(symbol="AAPL", trade_date=date(2024, 5, 31), close=1.0))
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("quote",), now=monday_now)
    assert report.scopes["quote"].fresh == ["AAPL"]
    assert report.scopes["quote"].stale == []


def test_quote_tuesday_with_friday_bar_is_stale(session: Session) -> None:
    # `now = Tuesday 2024-06-04`, Friday bar 2024-05-31. Diff = 4 days > 3.
    session.add(DailyPrice(symbol="AAPL", trade_date=date(2024, 5, 31), close=1.0))
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("quote",), now=NOW)
    assert report.scopes["quote"].stale == ["AAPL"]
    assert report.scopes["quote"].fresh == []


# ----- financials uses fiscal_date, not known_at ---------------------------


def test_financials_uses_fiscal_date_not_known_at(session: Session) -> None:
    # Recently re-ingested (known_at = now) but the underlying fiscal period
    # is 200 days old → must be `stale` (no new quarter has dropped).
    session.add(
        IncomeStatement(
            symbol="AAPL",
            period="FY",
            fiscal_date=NOW.date() - timedelta(days=200),
            known_at=NOW,  # very recent
            raw={},
        )
    )
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("financials",), now=NOW)
    assert report.scopes["financials"].stale == ["AAPL"]
    assert report.scopes["financials"].fresh == []


def test_financials_missing_when_no_row(session: Session) -> None:
    report = check_freshness(session, ["AAPL"], scopes=("financials",), now=NOW)
    assert report.scopes["financials"].missing == ["AAPL"]


# ----- transcript threshold (judged on call_date, 100d) --------------------


def test_transcript_fresh_when_call_date_within_100d(session: Session) -> None:
    session.add(
        Transcript(
            symbol="AAPL", year=2024, quarter=1,
            call_date=NOW.date() - timedelta(days=50),
        )
    )
    session.commit()
    report = check_freshness(session, ["AAPL"], scopes=("transcript",), now=NOW)
    assert report.scopes["transcript"].fresh == ["AAPL"]
    assert report.scopes["transcript"].threshold_days == 100


def test_transcript_stale_when_call_date_past_100d(session: Session) -> None:
    session.add(
        Transcript(
            symbol="AAPL", year=2023, quarter=1,
            call_date=NOW.date() - timedelta(days=120),
        )
    )
    session.commit()
    report = check_freshness(session, ["AAPL"], scopes=("transcript",), now=NOW)
    assert report.scopes["transcript"].stale == ["AAPL"]


def test_transcript_missing_when_no_row(session: Session) -> None:
    report = check_freshness(session, ["AAPL"], scopes=("transcript",), now=NOW)
    assert report.scopes["transcript"].missing == ["AAPL"]


def test_transcript_uses_call_date_not_known_at(session: Session) -> None:
    # Fresh ingest (known_at=now) but the call itself is 200d old → stale.
    session.add(
        Transcript(
            symbol="AAPL", year=2023, quarter=1,
            call_date=NOW.date() - timedelta(days=200), known_at=NOW,
        )
    )
    session.commit()
    report = check_freshness(session, ["AAPL"], scopes=("transcript",), now=NOW)
    assert report.scopes["transcript"].stale == ["AAPL"]


# ----- insider threshold (judged on filing_date, 30d) ----------------------


def test_insider_fresh_when_filing_within_30d(session: Session) -> None:
    session.add(
        InsiderTransaction(
            symbol="AAPL", accession_no="a", line_no=0,
            filing_date=NOW.date() - timedelta(days=10),
        )
    )
    session.commit()
    report = check_freshness(session, ["AAPL"], scopes=("insider",), now=NOW)
    assert report.scopes["insider"].fresh == ["AAPL"]
    assert report.scopes["insider"].threshold_days == 30


def test_insider_stale_when_filing_past_30d(session: Session) -> None:
    session.add(
        InsiderTransaction(
            symbol="AAPL", accession_no="a", line_no=0,
            filing_date=NOW.date() - timedelta(days=45),
        )
    )
    session.commit()
    report = check_freshness(session, ["AAPL"], scopes=("insider",), now=NOW)
    assert report.scopes["insider"].stale == ["AAPL"]


def test_insider_missing_when_no_row(session: Session) -> None:
    report = check_freshness(session, ["AAPL"], scopes=("insider",), now=NOW)
    assert report.scopes["insider"].missing == ["AAPL"]


# ----- stale_symbols helper ------------------------------------------------


def test_stale_symbols_returns_stale_union_missing(session: Session) -> None:
    # A: stale (40d old), B: fresh (5d), C: missing (no row).
    session.add(
        Profile(symbol="AAPL", known_at=NOW - timedelta(days=40), raw={})
    )
    session.add(
        Profile(symbol="MSFT", known_at=NOW - timedelta(days=5), raw={})
    )
    session.commit()

    out = stale_symbols(session, "profile", ["AAPL", "MSFT", "NVDA"], now=NOW)
    assert out == ["AAPL", "NVDA"]  # sorted union of stale + missing


# ----- additional safety nets ---------------------------------------------


def test_empty_symbols_returns_zero_total(session: Session) -> None:
    report = check_freshness(session, [], scopes=("profile",), now=NOW)
    sf = report.scopes["profile"]
    assert sf.total == 0
    assert sf.fresh == sf.stale == sf.missing == []


def test_unknown_scope_raises(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        check_freshness(session, ["AAPL"], scopes=("nonexistent",), now=NOW)
    with pytest.raises(ValueError, match="unknown scope"):
        stale_symbols(session, "nonexistent", ["AAPL"], now=NOW)


def test_all_default_scopes_present_in_report(session: Session) -> None:
    report = check_freshness(session, ["AAPL"], now=NOW)
    assert set(report.scopes.keys()) == set(SCOPE_THRESHOLDS.keys())


def test_threshold_days_surfaced(session: Session) -> None:
    report = check_freshness(session, ["AAPL"], scopes=("profile", "quote"), now=NOW)
    assert report.scopes["profile"].threshold_days == 30
    assert report.scopes["quote"].threshold_days == 3


def test_ratios_uses_known_at(session: Session) -> None:
    session.add(
        FinancialRatios(
            symbol="AAPL",
            period="FY",
            fiscal_date=NOW.date() - timedelta(days=200),  # ancient fiscal
            known_at=NOW - timedelta(days=2),  # fresh ingest
            raw={},
        )
    )
    session.commit()

    # ratios uses `known_at` (not fiscal_date like financials) → fresh.
    report = check_freshness(session, ["AAPL"], scopes=("ratios",), now=NOW)
    assert report.scopes["ratios"].fresh == ["AAPL"]


def test_estimates_uses_known_at(session: Session) -> None:
    session.add(
        AnalystEstimate(
            symbol="AAPL",
            fiscal_date=NOW.date() + timedelta(days=120),  # future forecast
            period="FY",
            known_at=NOW - timedelta(days=10),  # estimates threshold = 7d → stale
            raw={},
        )
    )
    session.commit()

    report = check_freshness(session, ["AAPL"], scopes=("estimates",), now=NOW)
    assert report.scopes["estimates"].stale == ["AAPL"]
