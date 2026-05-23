"""Factor registry + point-in-time fundamental gating + momentum math."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.signals.factors import (
    FactorError,
    _momentum_12_1,
    get_factor,
)
from quant_researcher.signals.panel import build_fundamental_panel, load_price_panel


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def test_unknown_factor_raises() -> None:
    with pytest.raises(FactorError, match="unknown factor"):
        get_factor("nope")


def test_fundamental_factor_reuses_fields_mapping() -> None:
    spec = get_factor("roe")
    assert spec.kind == "fundamental"
    assert spec.ratio_col == "return_on_equity"
    assert spec.direction == 1
    assert get_factor("pe").direction == -1


def test_price_factor_registered() -> None:
    assert get_factor("momentum_12_1").kind == "price"
    assert get_factor("momentum_12_1").price_fn is not None


def test_pit_fundamental_gating(session: Session) -> None:
    # FY2023 filed 2024-02-15 (roe 0.20); FY2024 filed 2025-02-14 (roe 0.30).
    # IncomeStatement.known_at = acceptedDate is the PIT truth (ratios.known_at ignored).
    session.add_all(
        [
            FinancialRatios(symbol="AAPL", period="FY", fiscal_date=date(2023, 12, 31),
                            return_on_equity=0.20, known_at=datetime(2024, 2, 15, tzinfo=UTC)),
            FinancialRatios(symbol="AAPL", period="FY", fiscal_date=date(2024, 12, 31),
                            return_on_equity=0.30, known_at=datetime(2025, 2, 14, tzinfo=UTC)),
            IncomeStatement(symbol="AAPL", period="FY", fiscal_date=date(2023, 12, 31),
                            known_at=datetime(2024, 2, 15, tzinfo=UTC)),
            IncomeStatement(symbol="AAPL", period="FY", fiscal_date=date(2024, 12, 31),
                            known_at=datetime(2025, 2, 14, tzinfo=UTC)),
        ]
    )
    session.commit()

    panel = build_fundamental_panel(
        session, ["AAPL"], [date(2024, 6, 30), date(2025, 6, 30)], "return_on_equity"
    )
    # 2024-06-30: FY2024 not yet filed → see FY2023 value
    assert panel[date(2024, 6, 30)]["AAPL"] == 0.20
    # 2025-06-30: FY2024 now filed → see the newer value
    assert panel[date(2025, 6, 30)]["AAPL"] == 0.30


def test_pit_excludes_unfiled(session: Session) -> None:
    # A filing whose acceptedDate is after the rebalance date must NOT be visible.
    session.add_all(
        [
            FinancialRatios(symbol="X", period="FY", fiscal_date=date(2024, 12, 31),
                            return_on_equity=0.5, known_at=datetime(2025, 2, 1, tzinfo=UTC)),
            IncomeStatement(symbol="X", period="FY", fiscal_date=date(2024, 12, 31),
                            known_at=datetime(2025, 2, 1, tzinfo=UTC)),
        ]
    )
    session.commit()
    panel = build_fundamental_panel(session, ["X"], [date(2025, 1, 15)], "return_on_equity")
    assert panel[date(2025, 1, 15)]["X"] is None  # filed 2025-02-01, after 2025-01-15


def test_momentum_12_1_offsets(session: Session) -> None:
    d0 = date(2023, 1, 2)
    for i in range(300):
        px = 100.0 + i
        session.add(DailyPrice(symbol="X", trade_date=d0 + timedelta(days=i),
                               close=px, adj_close=px))
    session.commit()
    series = load_price_panel(session, ["X"])["X"]
    anchor = d0 + timedelta(days=299)
    idx = series.index_on_or_before(anchor)
    expected = series.price_at_offset(idx, 21) / series.price_at_offset(idx, 252) - 1
    assert _momentum_12_1(series, anchor) == pytest.approx(expected)


def test_price_on_or_before_staleness(session: Session) -> None:
    d0 = date(2023, 1, 2)
    for i in range(5):
        session.add(DailyPrice(symbol="X", trade_date=d0 + timedelta(days=i),
                               close=100.0, adj_close=100.0))
    session.commit()
    series = load_price_panel(session, ["X"])["X"]
    last = d0 + timedelta(days=4)
    assert series.price_on_or_before(last) == 100.0
    # 10 days after the last bar → beyond the 3-day window → None
    assert series.price_on_or_before(last + timedelta(days=10)) is None
