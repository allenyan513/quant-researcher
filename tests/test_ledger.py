"""Ledger engine — record / track / scorecard end-to-end with seeded prices."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.ledger.engine import (
    HORIZON_DAYS,
    record_decision,
    scorecard,
    track_decisions,
)
from quant_researcher.ledger.sectors import etf_for_sector
from quant_researcher.models.decisions import Decision, DecisionTracking
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _seed_bar(session: Session, symbol: str, on: date, close: float) -> None:
    session.add(DailyPrice(symbol=symbol, trade_date=on, close=close))


def _seed_profile(session: Session, symbol: str, sector: str) -> None:
    session.add(
        Profile(symbol=symbol, sector=sector, raw={}, known_at=datetime.now(UTC))
    )


# ----- sector mapping ----------------------------------------------------


def test_etf_for_sector_known() -> None:
    assert etf_for_sector("Technology") == "XLK"
    assert etf_for_sector("Financial Services") == "XLF"
    assert etf_for_sector("Energy") == "XLE"


def test_etf_for_sector_falls_back_to_spy() -> None:
    assert etf_for_sector(None) == "SPY"
    assert etf_for_sector("UnknownSector") == "SPY"


# ----- record_decision ----------------------------------------------------


def test_record_decision_writes_row(session: Session) -> None:
    _seed_profile(session, "AAPL", "Technology")
    _seed_bar(session, "AAPL", date.today(), 200.0)
    session.commit()

    result = record_decision(
        session,
        symbol="AAPL",
        side="buy",
        thesis="growth story",
        confidence=4,
        tags=["AI", "tech"],
    )
    session.commit()

    row = session.get(Decision, result.decision_id)
    assert row is not None
    assert row.symbol == "AAPL"
    assert row.side == "buy"
    assert row.price_at_open == 200.0
    assert row.thesis == "growth story"
    assert row.confidence == 4
    assert row.tags == ["AI", "tech"]
    assert row.sector_at_open == "Technology"
    assert row.bundle_id is not None  # auto-bundle ran
    assert row.code_version is not None


def test_record_decision_rejects_bad_side(session: Session) -> None:
    with pytest.raises(ValueError, match="side must be"):
        record_decision(session, symbol="AAPL", side="hold")


def test_record_decision_skips_auto_bundle(session: Session) -> None:
    _seed_profile(session, "AAPL", "Technology")
    _seed_bar(session, "AAPL", date.today(), 200.0)
    session.commit()
    result = record_decision(
        session, symbol="AAPL", side="buy", auto_bundle=False
    )
    session.commit()
    row = session.get(Decision, result.decision_id)
    assert row.bundle_id is None


# ----- track_decisions ----------------------------------------------------


def test_track_decisions_computes_returns(session: Session) -> None:
    """Time-travel scenario: decision opened 60 days ago, 1w / 1m horizons elapsed."""
    opened = date(2026, 3, 1)
    # Symbol AAPL: +20% by day 7, +50% by day 30
    _seed_bar(session, "AAPL", opened, 100.0)
    _seed_bar(session, "AAPL", opened + timedelta(days=7), 120.0)
    _seed_bar(session, "AAPL", opened + timedelta(days=30), 150.0)
    # SPY: +5% by day 7, +10% by day 30
    _seed_bar(session, "SPY", opened, 400.0)
    _seed_bar(session, "SPY", opened + timedelta(days=7), 420.0)
    _seed_bar(session, "SPY", opened + timedelta(days=30), 440.0)
    # XLK: +10% by day 7, +20% by day 30
    _seed_bar(session, "XLK", opened, 200.0)
    _seed_bar(session, "XLK", opened + timedelta(days=7), 220.0)
    _seed_bar(session, "XLK", opened + timedelta(days=30), 240.0)
    _seed_profile(session, "AAPL", "Technology")
    session.commit()

    rec = record_decision(
        session,
        symbol="AAPL",
        side="buy",
        opened_at=opened,
        auto_bundle=False,
        confidence=3,
    )
    session.commit()

    result = track_decisions(session, as_of=opened + timedelta(days=40))
    session.commit()

    # 1w and 1m elapsed (7 and 30 days); 3m and 6m not.
    assert result.rows_written == 2
    assert result.rows_skipped_horizon_not_elapsed == 2

    rows = list(
        session.scalars(
            select(DecisionTracking)
            .where(DecisionTracking.decision_id == rec.decision_id)
            .order_by(DecisionTracking.horizon)
        )
    )
    assert {r.horizon for r in rows} == {"1m", "1w"}

    one_week = next(r for r in rows if r.horizon == "1w")
    assert one_week.return_pct == pytest.approx(0.20)  # 100 → 120
    assert one_week.spy_return_pct == pytest.approx(0.05)
    assert one_week.sector_return_pct == pytest.approx(0.10)
    # alpha vs sector: 0.20 - 0.10 = 0.10
    assert one_week.alpha_pct == pytest.approx(0.10)

    one_month = next(r for r in rows if r.horizon == "1m")
    assert one_month.return_pct == pytest.approx(0.50)
    assert one_month.alpha_pct == pytest.approx(0.30)


def test_track_decisions_sell_flips_sign(session: Session) -> None:
    opened = date(2026, 3, 1)
    _seed_bar(session, "AAPL", opened, 100.0)
    _seed_bar(session, "AAPL", opened + timedelta(days=7), 80.0)  # -20% but short
    _seed_bar(session, "SPY", opened, 400.0)
    _seed_bar(session, "SPY", opened + timedelta(days=7), 400.0)
    _seed_profile(session, "AAPL", "Technology")
    session.commit()
    rec = record_decision(
        session, symbol="AAPL", side="sell", opened_at=opened, auto_bundle=False
    )
    session.commit()
    track_decisions(session, as_of=opened + timedelta(days=10))
    session.commit()
    row = session.scalars(
        select(DecisionTracking).where(
            DecisionTracking.decision_id == rec.decision_id,
            DecisionTracking.horizon == "1w",
        )
    ).one()
    # Short: price dropped 20% → +20% return for the decision.
    assert row.return_pct == pytest.approx(0.20)


def test_track_decisions_missing_prices_yield_none(session: Session) -> None:
    opened = date(2026, 3, 1)
    _seed_profile(session, "AAPL", "Technology")
    _seed_bar(session, "AAPL", opened, 100.0)
    # No future price; SPY/XLK absent.
    session.commit()
    rec = record_decision(
        session, symbol="AAPL", side="buy", opened_at=opened, auto_bundle=False
    )
    session.commit()
    track_decisions(session, as_of=opened + timedelta(days=40))
    session.commit()
    rows = list(
        session.scalars(
            select(DecisionTracking).where(
                DecisionTracking.decision_id == rec.decision_id
            )
        )
    )
    assert rows  # rows still written (price column may be None)
    for r in rows:
        assert r.return_pct is None  # no forward AAPL bar
        assert r.alpha_pct is None  # can't compute


def test_track_decisions_idempotent(session: Session) -> None:
    opened = date(2026, 3, 1)
    _seed_bar(session, "AAPL", opened, 100.0)
    _seed_bar(session, "AAPL", opened + timedelta(days=7), 120.0)
    _seed_profile(session, "AAPL", "Technology")
    session.commit()
    record_decision(
        session, symbol="AAPL", side="buy", opened_at=opened, auto_bundle=False
    )
    session.commit()
    r1 = track_decisions(session, as_of=opened + timedelta(days=10))
    session.commit()
    r2 = track_decisions(session, as_of=opened + timedelta(days=10))
    session.commit()
    # Same horizons get overwritten, not duplicated.
    rows = list(session.scalars(select(DecisionTracking)))
    assert len(rows) == 1  # only 1w elapsed
    assert r1.rows_written == r2.rows_written


# ----- scorecard ----------------------------------------------------------


def test_scorecard_by_confidence(session: Session) -> None:
    opened = date(2026, 3, 1)
    for sym, close7, _conf in (
        ("A", 110.0, 5),  # +10%
        ("B", 95.0, 5),   # -5%
        ("C", 130.0, 3),  # +30%
    ):
        _seed_bar(session, sym, opened, 100.0)
        _seed_bar(session, sym, opened + timedelta(days=7), close7)
        _seed_profile(session, sym, "Technology")
    _seed_bar(session, "SPY", opened, 400.0)
    _seed_bar(session, "SPY", opened + timedelta(days=7), 400.0)
    _seed_bar(session, "XLK", opened, 200.0)
    _seed_bar(session, "XLK", opened + timedelta(days=7), 200.0)
    session.commit()

    for sym, conf in (("A", 5), ("B", 5), ("C", 3)):
        record_decision(
            session,
            symbol=sym,
            side="buy",
            opened_at=opened,
            confidence=conf,
            auto_bundle=False,
        )
    session.commit()
    track_decisions(session, as_of=opened + timedelta(days=10))
    session.commit()

    rows = scorecard(session, group_by="confidence", horizon="1w")
    assert len(rows) == 2  # confidence 5 + confidence 3
    conf5 = next(r for r in rows if r["group"] == "5")
    conf3 = next(r for r in rows if r["group"] == "3")
    # conf5: avg return = (10% + -5%) / 2 = 2.5%; alpha same since benchmarks 0
    assert conf5["decision_count"] == 2
    assert conf5["avg_return_pct"] == pytest.approx(0.025)
    assert conf3["avg_return_pct"] == pytest.approx(0.30)


def test_scorecard_by_tag_splits_multi_tag(session: Session) -> None:
    opened = date(2026, 3, 1)
    _seed_bar(session, "A", opened, 100.0)
    _seed_bar(session, "A", opened + timedelta(days=7), 110.0)
    _seed_bar(session, "SPY", opened, 400.0)
    _seed_bar(session, "SPY", opened + timedelta(days=7), 400.0)
    _seed_profile(session, "A", "Technology")
    _seed_bar(session, "XLK", opened, 200.0)
    _seed_bar(session, "XLK", opened + timedelta(days=7), 200.0)
    session.commit()
    record_decision(
        session,
        symbol="A",
        side="buy",
        opened_at=opened,
        tags=["AI", "growth"],
        auto_bundle=False,
    )
    session.commit()
    track_decisions(session, as_of=opened + timedelta(days=10))
    session.commit()
    rows = scorecard(session, group_by="tag", horizon="1w")
    # Single decision shows up under both tags.
    tags = {r["group"] for r in rows}
    assert tags == {"AI", "growth"}


def test_scorecard_rejects_bad_group_by(session: Session) -> None:
    with pytest.raises(ValueError, match="group_by"):
        scorecard(session, group_by="bogus")


def test_scorecard_rejects_bad_horizon(session: Session) -> None:
    with pytest.raises(ValueError, match="horizon"):
        scorecard(session, horizon="1y")


def test_horizon_days_keys() -> None:
    assert set(HORIZON_DAYS.keys()) == {"1w", "1m", "3m", "6m"}
