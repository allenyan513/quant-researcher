"""qr earnings — actual-vs-estimate join, sparsity handling, thesis, CLI."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base
from quant_researcher.models.decisions import Decision
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.profile import Profile
from quant_researcher.research.earnings import read_earnings

runner = CliRunner()


def _seed(session: Session) -> None:
    # FY2024: actual + matching estimate (→ surprise). FY2023: actual, no estimate.
    session.add_all(
        [
            IncomeStatement(symbol="AAPL", period="FY", fiscal_date=date(2024, 9, 30),
                            revenue=391000.0, net_income=94000.0, eps=6.1, eps_diluted=6.0,
                            gross_profit=180000.0, operating_income=120000.0,
                            known_at=datetime(2024, 11, 1, tzinfo=UTC)),
            IncomeStatement(symbol="AAPL", period="FY", fiscal_date=date(2023, 9, 30),
                            revenue=383000.0, net_income=97000.0, eps=6.2, eps_diluted=6.1,
                            gross_profit=170000.0, operating_income=114000.0,
                            known_at=datetime(2023, 11, 1, tzinfo=UTC)),
            AnalystEstimate(symbol="AAPL", fiscal_date=date(2024, 9, 30), period="FY",
                            revenue_avg=385000.0, eps_avg=5.5, num_analysts_eps=30,
                            known_at=datetime(2024, 6, 1, tzinfo=UTC)),
            Decision(decision_id="d1", symbol="AAPL", side="buy",
                     opened_at=date(2024, 1, 1), thesis="services margin expansion",
                     confidence=4),
        ]
    )
    session.commit()


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def test_read_earnings_computes_surprise_when_estimate_present(session: Session) -> None:
    _seed(session)
    out = read_earnings(session, "AAPL")

    assert out["periods_found"] == 2
    assert out["estimates_matched"] == 1
    fy24 = out["periods"][0]  # newest first
    assert fy24["fiscal_date"] == "2024-09-30"
    assert fy24["estimate_available"] is True
    # eps surprise = (6.0 - 5.5)/5.5*100 ≈ 9.09
    assert fy24["surprise"]["eps_surprise_pct"] == pytest.approx(9.0909, abs=0.01)
    assert fy24["surprise"]["revenue_surprise_pct"] == pytest.approx(1.558, abs=0.01)
    assert fy24["filed_at"] is not None  # acceptedDate surfaced


def test_read_earnings_handles_missing_estimate(session: Session) -> None:
    _seed(session)
    out = read_earnings(session, "AAPL")
    fy23 = out["periods"][1]
    assert fy23["fiscal_date"] == "2023-09-30"
    assert fy23["estimate_available"] is False
    assert fy23["surprise"] is None
    assert "estimate unavailable" in fy23["note"]


def test_read_earnings_surfaces_thesis(session: Session) -> None:
    _seed(session)
    out = read_earnings(session, "AAPL")
    assert out["thesis"]["count"] == 1
    assert out["thesis"]["decisions"][0]["thesis"] == "services margin expansion"


def test_read_earnings_thin_data(session: Session) -> None:
    out = read_earnings(session, "NOPE")
    assert out["periods_found"] == 0
    assert any("no financial statements" in n for n in out["notes"])


def test_read_earnings_uses_net_revenue_for_banks(session: Session) -> None:
    # Bank revenue surprise must compare net-vs-net (issue #36).
    # FMP gross revenue 125 − interestExpense 67 = 58 (net). Estimate
    # was 56 (net). Surprise = (58-56)/56*100 ≈ +3.57% — NOT +123%
    # which is what the broken gross-vs-net comparison would give.
    session.add_all([
        Profile(
            symbol="JPM",
            sector="Financial Services",
            industry="Banks - Diversified",
            raw={},
            known_at=datetime(2024, 11, 1, tzinfo=UTC),
        ),
        IncomeStatement(
            symbol="JPM",
            period="FY",
            fiscal_date=date(2024, 12, 31),
            revenue=125e9,
            net_income=58e9,
            eps_diluted=20.0,
            eps=20.0,
            known_at=datetime(2025, 2, 1, tzinfo=UTC),
            raw={"interestExpense": 67e9},
        ),
        AnalystEstimate(
            symbol="JPM",
            fiscal_date=date(2024, 12, 31),
            period="FY",
            revenue_avg=56e9,
            eps_avg=19.0,
            num_analysts_eps=25,
            known_at=datetime(2024, 6, 1, tzinfo=UTC),
        ),
    ])
    session.commit()

    out = read_earnings(session, "JPM")
    fy = out["periods"][0]
    # Revenue surprise computed on NET revenue (58) vs analyst (56).
    assert fy["surprise"]["revenue_surprise_pct"] == pytest.approx(
        (58e9 - 56e9) / 56e9 * 100, abs=0.01
    )
    # Sanity: would have been ~+123% under the broken gross-vs-net comparison.
    assert fy["surprise"]["revenue_surprise_pct"] < 10  # clearly not the broken value
    # `actual.revenue` keeps the raw FMP-gross line for transparency.
    assert fy["actual"]["revenue"] == 125e9
    # `actual.revenue_net` is the derived net number (only emitted for banks).
    assert fy["actual"]["revenue_net"] == 58e9


def test_read_earnings_general_stock_unaffected(session: Session) -> None:
    # AAPL (non-bank) — Profile with sector=Technology, or no profile.
    # Either way revenue_net must NOT be added (it would clutter the
    # general-template output).
    _seed(session)
    out = read_earnings(session, "AAPL")
    assert "revenue_net" not in out["periods"][0]["actual"]


def test_read_earnings_includes_transcript_excerpt(session: Session) -> None:
    _seed(session)
    out = read_earnings(session, "AAPL", transcript_excerpt="call text...")
    assert out["transcript"] == {"available": True, "excerpt": "call text..."}


# ----- CLI single-envelope --------------------------------------------------


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as sess:
        _seed(sess)
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


def test_earnings_cli_single_envelope(memory_db) -> None:
    result = runner.invoke(app, ["earnings", "AAPL"])
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}"
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["periods_found"] == 2
    assert payloads[0]["data"]["estimates_matched"] == 1
    assert result.exit_code == 0
