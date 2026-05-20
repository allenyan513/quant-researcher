"""refresh.py — profile + quotes refresh logic with a mocked FMP client."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.data.refresh import refresh_profile, refresh_quotes
from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


@pytest.fixture
def fmp() -> MagicMock:
    return MagicMock(spec=FMPClient)


# ----- profile --------------------------------------------------------------


def test_refresh_profile_inserts_new_row(session: Session, fmp: MagicMock) -> None:
    fmp.get_profile.return_value = {
        "symbol": "AAPL",
        "companyName": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchangeShortName": "NASDAQ",
        "currency": "USD",
        "country": "US",
        "beta": 1.23,
        "ipoDate": "1980-12-12",
        "isEtf": False,
        "isFund": False,
        "isAdr": False,
        "isActivelyTrading": True,
    }
    result = refresh_profile(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 1
    assert result.failed == []
    assert result.succeeded == ["AAPL"]

    row = session.get(Profile, "AAPL")
    assert row is not None
    assert row.company_name == "Apple Inc."
    assert row.sector == "Technology"
    assert row.exchange == "NASDAQ"
    assert row.beta == 1.23
    assert row.ipo_date == date(1980, 12, 12)
    assert row.is_actively_trading is True
    assert row.raw["symbol"] == "AAPL"
    assert row.known_at is not None


def test_refresh_profile_overwrites_existing(session: Session, fmp: MagicMock) -> None:
    fmp.get_profile.return_value = {"symbol": "AAPL", "sector": "Tech"}
    refresh_profile(session, fmp, ["AAPL"])
    session.commit()

    fmp.get_profile.return_value = {"symbol": "AAPL", "sector": "Updated"}
    refresh_profile(session, fmp, ["AAPL"])
    session.commit()

    row = session.get(Profile, "AAPL")
    assert row is not None
    assert row.sector == "Updated"


def test_refresh_profile_empty_response_marks_failure(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_profile.return_value = None
    result = refresh_profile(session, fmp, ["NOPE"])
    session.commit()

    assert result.total_upserted == 0
    assert len(result.failed) == 1
    assert result.failed[0]["symbol"] == "NOPE"
    assert "empty" in result.failed[0]["error"]


def test_refresh_profile_isolates_per_symbol_errors(
    session: Session, fmp: MagicMock
) -> None:
    def side_effect(symbol: str) -> dict:
        if symbol == "BAD":
            raise FMPError("simulated 404", status_code=404)
        return {"symbol": symbol, "sector": "X"}

    fmp.get_profile.side_effect = side_effect
    result = refresh_profile(session, fmp, ["AAPL", "BAD", "MSFT"])
    session.commit()

    assert sorted(result.succeeded) == ["AAPL", "MSFT"]
    assert [f["symbol"] for f in result.failed] == ["BAD"]
    assert session.get(Profile, "AAPL") is not None
    assert session.get(Profile, "MSFT") is not None
    assert session.get(Profile, "BAD") is None


def test_refresh_profile_handles_invalid_ipo_date(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_profile.return_value = {"symbol": "AAPL", "ipoDate": "not-a-date"}
    result = refresh_profile(session, fmp, ["AAPL"])
    session.commit()

    assert result.succeeded == ["AAPL"]
    row = session.get(Profile, "AAPL")
    assert row is not None
    assert row.ipo_date is None


# ----- quotes ---------------------------------------------------------------


def test_refresh_quotes_initial_fetch_uses_lookback_window(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_historical_prices.return_value = [
        {
            "date": "2024-01-02",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "adjClose": 1.5,
            "volume": 100,
        },
        {
            "date": "2024-01-03",
            "open": 1.5,
            "high": 2.5,
            "low": 1.0,
            "close": 2.0,
            "adjClose": 2.0,
            "volume": 200,
        },
    ]
    result = refresh_quotes(session, fmp, ["AAPL"], lookback_days=30)
    session.commit()

    assert result.total_upserted == 2
    assert result.total_skipped == 0
    assert fmp.get_historical_prices.call_args.kwargs["since"] == date.today() - timedelta(
        days=30
    )

    rows = sorted(
        session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")),
        key=lambda r: r.trade_date,
    )
    assert [r.trade_date for r in rows] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert rows[0].close == 1.5
    assert rows[0].volume == 100


def test_refresh_quotes_incremental_uses_latest_plus_one(
    session: Session, fmp: MagicMock
) -> None:
    session.execute(
        insert(DailyPrice),
        [{"symbol": "AAPL", "trade_date": date(2024, 6, 1), "close": 1.0}],
    )
    session.commit()

    fmp.get_historical_prices.return_value = [
        {"date": "2024-06-02", "close": 1.1, "volume": 100},
    ]
    refresh_quotes(session, fmp, ["AAPL"])

    assert fmp.get_historical_prices.call_args.kwargs["since"] == date(2024, 6, 2)


def test_refresh_quotes_dedupes(session: Session, fmp: MagicMock) -> None:
    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 1.5, "volume": 100},
    ]
    refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 1.5, "volume": 100},
    ]
    result = refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 0
    assert result.total_skipped == 1
    rows = list(session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")))
    assert len(rows) == 1


def test_refresh_quotes_empty_response(session: Session, fmp: MagicMock) -> None:
    fmp.get_historical_prices.return_value = []
    result = refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 0
    assert result.failed == []
    assert result.succeeded == ["AAPL"]


def test_refresh_quotes_isolates_errors(session: Session, fmp: MagicMock) -> None:
    def side_effect(symbol: str, *, since=None) -> list[dict]:
        if symbol == "BAD":
            raise FMPError("rate limited", status_code=429)
        return [{"date": "2024-01-02", "close": 1.0}]

    fmp.get_historical_prices.side_effect = side_effect
    result = refresh_quotes(session, fmp, ["AAPL", "BAD", "MSFT"])
    session.commit()

    assert sorted(result.succeeded) == ["AAPL", "MSFT"]
    assert [f["symbol"] for f in result.failed] == ["BAD"]


def test_refresh_quotes_skips_rows_without_date(session: Session, fmp: MagicMock) -> None:
    fmp.get_historical_prices.return_value = [
        {"date": None, "close": 1.0},
        {"date": "invalid", "close": 1.0},
        {"date": "2024-01-02", "close": 1.5, "volume": 100},
    ]
    result = refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 1
    rows = list(session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")))
    assert len(rows) == 1
    assert rows[0].trade_date == date(2024, 1, 2)
