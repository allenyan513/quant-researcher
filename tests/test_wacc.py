"""WACC computation — Bloomberg adjustment + CAPM."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.profile import Profile
from quant_researcher.valuation.wacc import (
    DEFAULT_BETA_FALLBACK,
    bloomberg_adjusted_beta,
    simple_wacc,
    wacc_for_symbol,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def test_bloomberg_adjustment_pulls_high_beta_toward_one() -> None:
    # β=2.0 → adj = 2/3 * 2 + 1/3 = 5/3 ≈ 1.667
    assert bloomberg_adjusted_beta(2.0) == pytest.approx(5 / 3, rel=1e-6)


def test_bloomberg_adjustment_pulls_low_beta_toward_one() -> None:
    # β=0.5 → adj = 2/3 * 0.5 + 1/3 = 2/3
    assert bloomberg_adjusted_beta(0.5) == pytest.approx(2 / 3, rel=1e-6)


def test_bloomberg_adjustment_identity_at_one() -> None:
    assert bloomberg_adjusted_beta(1.0) == pytest.approx(1.0)


def test_bloomberg_adjustment_fallback_when_none() -> None:
    assert bloomberg_adjusted_beta(None) == DEFAULT_BETA_FALLBACK


def test_simple_wacc_default_inputs() -> None:
    # β=1 → adj=1 → wacc = 4.5% + 1 * 5.5% = 10%
    assert simple_wacc(1.0) == pytest.approx(0.10)


def test_simple_wacc_high_beta_higher_wacc() -> None:
    low = simple_wacc(0.5)
    high = simple_wacc(2.0)
    assert high > low


def test_simple_wacc_custom_inputs() -> None:
    # β=1 → wacc = 5% + 1 * 6% = 11%
    assert simple_wacc(1.0, rf=0.05, erp=0.06) == pytest.approx(0.11)


def test_wacc_for_symbol_reads_beta(session: Session) -> None:
    session.add(
        Profile(symbol="AAPL", beta=1.2, raw={}, known_at=datetime.now(UTC))
    )
    session.commit()
    w, breakdown = wacc_for_symbol(session, "AAPL")
    # adj = 2/3 * 1.2 + 1/3 = 1.133; wacc = 0.045 + 1.133 * 0.055 = 0.107...
    assert w == pytest.approx(0.045 + (2 / 3 * 1.2 + 1 / 3) * 0.055)
    assert breakdown["raw_beta"] == 1.2
    assert breakdown["wacc"] == w


def test_wacc_for_symbol_fallback_when_no_beta(session: Session) -> None:
    session.add(
        Profile(symbol="AAPL", beta=None, raw={}, known_at=datetime.now(UTC))
    )
    session.commit()
    w, breakdown = wacc_for_symbol(session, "AAPL")
    # adj = 1.0 (fallback); wacc = 0.10
    assert w == pytest.approx(0.10)
    assert breakdown["raw_beta"] is None
    assert breakdown["adj_beta"] == 1.0


def test_wacc_for_symbol_unknown_returns_default(session: Session) -> None:
    w, _ = wacc_for_symbol(session, "GHOST")
    assert w == pytest.approx(0.10)
