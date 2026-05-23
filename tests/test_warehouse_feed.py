"""WarehouseDataFeed — daily_prices → Bar, with split/dividend adjustment."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.engine.data import WarehouseDataFeed
from quant_researcher.models.prices import DailyPrice


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _add(session: Session, sym: str, d: date, **kw: object) -> None:
    defaults = dict(open=10.0, high=10.0, low=10.0, close=10.0, adj_close=10.0, volume=1_000)
    defaults.update(kw)
    session.add(DailyPrice(symbol=sym, trade_date=d, **defaults))


def test_returns_bars_in_date_order(session: Session) -> None:
    for d in (date(2023, 1, 5), date(2023, 1, 3), date(2023, 1, 4)):  # out of order
        _add(session, "AAPL", d)
    session.commit()

    bars = WarehouseDataFeed(session).fetch("AAPL", "2023-01-01", "2023-12-31")
    assert [b.timestamp.date() for b in bars] == [
        date(2023, 1, 3),
        date(2023, 1, 4),
        date(2023, 1, 5),
    ]


def test_back_adjusts_ohlc_by_adj_close_ratio(session: Session) -> None:
    # close=100, adj_close=50 → factor 0.5 applied to O/H/L; close becomes adj.
    _add(session, "X", date(2023, 1, 3), open=100, high=110, low=90, close=100, adj_close=50)
    session.commit()

    bar = WarehouseDataFeed(session).fetch("X", "2023-01-01", "2023-12-31")[0]
    assert bar.close == 50.0
    assert bar.open == 50.0
    assert bar.high == 55.0
    assert bar.low == 45.0


def test_raw_mode_keeps_unadjusted(session: Session) -> None:
    _add(session, "X", date(2023, 1, 3), open=100, high=110, low=90, close=100, adj_close=50)
    session.commit()

    bar = WarehouseDataFeed(session, adjusted=False).fetch("X", "2023-01-01", "2023-12-31")[0]
    assert bar.close == 100.0
    assert bar.open == 100.0
    assert bar.high == 110.0


def test_falls_back_to_close_when_adj_missing(session: Session) -> None:
    _add(session, "X", date(2023, 1, 3), open=100, high=110, low=90, close=100, adj_close=None)
    session.commit()

    bar = WarehouseDataFeed(session).fetch("X", "2023-01-01", "2023-12-31")[0]
    assert bar.close == 100.0
    assert bar.open == 100.0  # factor 1.0


def test_filters_to_date_window(session: Session) -> None:
    for d in (date(2022, 12, 31), date(2023, 1, 3), date(2023, 6, 1), date(2024, 1, 1)):
        _add(session, "X", d)
    session.commit()

    bars = WarehouseDataFeed(session).fetch("X", "2023-01-01", "2023-12-31")
    assert [b.timestamp.date() for b in bars] == [date(2023, 1, 3), date(2023, 6, 1)]


def test_skips_rows_without_close(session: Session) -> None:
    _add(session, "X", date(2023, 1, 3), close=None, adj_close=None)
    _add(session, "X", date(2023, 1, 4), close=10, adj_close=10)
    session.commit()

    bars = WarehouseDataFeed(session).fetch("X", "2023-01-01", "2023-12-31")
    assert len(bars) == 1
    assert bars[0].timestamp.date() == date(2023, 1, 4)


def test_missing_volume_becomes_zero(session: Session) -> None:
    _add(session, "X", date(2023, 1, 3), volume=None)
    session.commit()

    bar = WarehouseDataFeed(session).fetch("X", "2023-01-01", "2023-12-31")[0]
    assert bar.volume == 0
