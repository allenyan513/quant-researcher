"""refresh.py — profile + quotes refresh logic with a mocked FMP client."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from quant_researcher.data.alphavantage import AlphaVantageClient, AlphaVantageError
from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.data.refresh import (
    _as_datetime,
    refresh_estimates,
    refresh_financials,
    refresh_profile,
    refresh_quotes,
    refresh_ratios,
    refresh_transcript,
)
from quant_researcher.db import Base
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
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


@pytest.fixture
def fmp() -> MagicMock:
    client = MagicMock(spec=FMPClient)
    # refresh_quotes always calls the adjusted endpoint too; default empty so
    # quote tests that only program get_historical_prices don't blow up.
    client.get_adjusted_prices.return_value = []
    return client


@pytest.fixture
def av() -> MagicMock:
    return MagicMock(spec=AlphaVantageClient)


def _naive_utc(dt: datetime) -> datetime:
    """SQLite drops tz on `DateTime(timezone=True)` — normalize both sides
    so cross-dialect tests compare equal. Postgres preserves tz; either way
    we strip it for the assertion."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


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
    # First insert (fresh profile lands).
    fmp.get_profile.return_value = {"symbol": "AAPL", "sector": "Tech"}
    refresh_profile(session, fmp, ["AAPL"])
    session.commit()

    # Force-refresh path: tests the overwrite semantic itself. (Default
    # `only_stale=True` would skip — the row was just inserted, < 30d old.)
    fmp.get_profile.return_value = {"symbol": "AAPL", "sector": "Updated"}
    refresh_profile(session, fmp, ["AAPL"], only_stale=False)
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


def test_refresh_quotes_incremental_refetches_heal_window(
    session: Session, fmp: MagicMock
) -> None:
    # Incremental fetch starts heal_window_days BEFORE the latest stored bar
    # (not latest+1) so the recent tail can be re-pulled and corrected.
    session.execute(
        insert(DailyPrice),
        [{"symbol": "AAPL", "trade_date": date(2024, 6, 1), "close": 1.0}],
    )
    session.commit()

    fmp.get_historical_prices.return_value = [
        {"date": "2024-06-02", "close": 1.1, "volume": 100},
    ]
    refresh_quotes(session, fmp, ["AAPL"], heal_window_days=5, only_stale=False)

    window_start = date(2024, 6, 1) - timedelta(days=5)
    assert fmp.get_historical_prices.call_args.kwargs["since"] == window_start
    # Adjusted stream re-fetched over the same window so heals carry adj_close.
    assert fmp.get_adjusted_prices.call_args.kwargs["since"] == window_start


def test_refresh_quotes_heals_preliminary_recent_bar(
    session: Session, fmp: MagicMock
) -> None:
    # A recent bar stored with a PRELIMINARY close/volume/adj_close (captured
    # near the US close) is re-fetched and overwritten in place once FMP
    # finalizes it — no duplicate row. Mirrors the real AAPL 2026-05-21 case.
    session.execute(
        insert(DailyPrice),
        [
            {
                "symbol": "AAPL",
                "trade_date": date(2024, 1, 10),
                "open": 300.0,
                "high": 305.0,
                "low": 299.0,
                "close": 302.25,
                "adj_close": 301.0,
                "volume": 7_412_155,
            }
        ],
    )
    session.commit()

    # /full now returns the finalized 2024-01-10 bar plus a brand-new 2024-01-11.
    fmp.get_historical_prices.return_value = [
        {
            "date": "2024-01-10",
            "open": 300.0,
            "high": 306.0,
            "low": 299.0,
            "close": 304.99,
            "volume": 42_965_126,
        },
        {"date": "2024-01-11", "close": 306.0, "volume": 1_000},
    ]
    fmp.get_adjusted_prices.return_value = [
        {"date": "2024-01-10", "adjClose": 303.5},
        {"date": "2024-01-11", "adjClose": 306.0},
    ]
    result = refresh_quotes(session, fmp, ["AAPL"], heal_window_days=7, only_stale=False)
    session.commit()

    rows = sorted(
        session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")),
        key=lambda r: r.trade_date,
    )
    assert len(rows) == 2  # healed in place, not duplicated
    healed, inserted = rows
    assert healed.trade_date == date(2024, 1, 10)
    assert healed.close == 304.99  # corrected from preliminary 302.25
    assert healed.volume == 42_965_126  # corrected from 7_412_155
    assert healed.adj_close == 303.5  # adj_close carried through the heal
    assert inserted.trade_date == date(2024, 1, 11)
    assert inserted.close == 306.0
    assert result.total_upserted == 2  # 1 heal + 1 insert
    assert result.total_skipped == 0


def test_refresh_quotes_old_bar_stays_insert_only(
    session: Session, fmp: MagicMock
) -> None:
    # An existing bar OLDER than the heal window is immutable: even if FMP
    # returns a different value for that date, it's skipped (not overwritten).
    # latest=2024-02-01, heal_window_days=7 → heal_floor=2024-01-25, so the
    # 2024-01-02 bar is outside the window.
    session.execute(
        insert(DailyPrice),
        [
            {"symbol": "AAPL", "trade_date": date(2024, 1, 2), "close": 1.0, "volume": 5},
            {"symbol": "AAPL", "trade_date": date(2024, 2, 1), "close": 2.0, "volume": 9},
        ],
    )
    session.commit()

    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 999.0, "volume": 123},  # old date, changed
    ]
    result = refresh_quotes(session, fmp, ["AAPL"], heal_window_days=7, only_stale=False)
    session.commit()

    old = session.get(DailyPrice, ("AAPL", date(2024, 1, 2)))
    assert old.close == 1.0  # NOT overwritten — insert-only for old bars
    assert old.volume == 5
    assert result.total_upserted == 0
    assert result.total_skipped == 1


def test_refresh_quotes_heal_keeps_stored_adj_close_when_adjusted_lags(
    session: Session, fmp: MagicMock
) -> None:
    # On a heal, if the dividend-adjusted stream doesn't return the bar's date
    # (it can lag /full), the stored adj_close is kept rather than nulled.
    session.execute(
        insert(DailyPrice),
        [
            {
                "symbol": "AAPL",
                "trade_date": date(2024, 1, 10),
                "close": 302.25,
                "adj_close": 301.0,
                "volume": 100,
            }
        ],
    )
    session.commit()

    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-10", "close": 304.99, "volume": 200},  # /full only
    ]
    fmp.get_adjusted_prices.return_value = []  # adjusted stream lagging
    refresh_quotes(session, fmp, ["AAPL"], heal_window_days=7, only_stale=False)
    session.commit()

    row = session.get(DailyPrice, ("AAPL", date(2024, 1, 10)))
    assert row.close == 304.99  # close still healed
    assert row.adj_close == 301.0  # prior adj_close preserved, not nulled


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


def test_refresh_quotes_populates_adj_close_from_adjusted_endpoint(
    session: Session, fmp: MagicMock
) -> None:
    # /full carries no adjClose (matches real FMP /stable); adj_close must come
    # from the dividend-adjusted stream, joined by date.
    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 100},
        {"date": "2024-01-03", "open": 10.5, "high": 12.0, "low": 10.0,
         "close": 11.0, "volume": 200},
    ]
    fmp.get_adjusted_prices.return_value = [
        {"date": "2024-01-02", "adjClose": 9.8, "volume": 100},
        {"date": "2024-01-03", "adjClose": 10.3, "volume": 200},
    ]
    result = refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 2
    rows = sorted(
        session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")),
        key=lambda r: r.trade_date,
    )
    assert [r.close for r in rows] == [10.5, 11.0]
    assert [r.adj_close for r in rows] == [9.8, 10.3]
    # Adjusted stream fetched over the same window as /full.
    assert (
        fmp.get_adjusted_prices.call_args.kwargs["since"]
        == fmp.get_historical_prices.call_args.kwargs["since"]
    )


def test_refresh_quotes_adj_close_none_when_no_adjusted_match(
    session: Session, fmp: MagicMock
) -> None:
    # No adjusted row for the bar's date → adj_close stays None (panel falls
    # back to raw close); the raw bar is still inserted.
    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 10.5, "volume": 100},
    ]
    fmp.get_adjusted_prices.return_value = []
    refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    row = session.scalar(select(DailyPrice).where(DailyPrice.symbol == "AAPL"))
    assert row.close == 10.5
    assert row.adj_close is None


def test_refresh_quotes_adj_close_partial_overlap(
    session: Session, fmp: MagicMock
) -> None:
    # Adjusted stream covers only some /full dates → matched bars get adj_close,
    # unmatched bars stay None. Guards against join-key (date) drift.
    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 10.0, "volume": 100},
        {"date": "2024-01-03", "close": 11.0, "volume": 100},
        {"date": "2024-01-04", "close": 12.0, "volume": 100},
    ]
    fmp.get_adjusted_prices.return_value = [
        {"date": "2024-01-02", "adjClose": 9.5},
        {"date": "2024-01-04", "adjClose": 11.5},
    ]
    refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    rows = sorted(
        session.scalars(select(DailyPrice).where(DailyPrice.symbol == "AAPL")),
        key=lambda r: r.trade_date,
    )
    assert [r.trade_date for r in rows] == [
        date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    ]
    assert [r.adj_close for r in rows] == [9.5, None, 11.5]


def test_refresh_quotes_adjusted_endpoint_failure_fails_symbol(
    session: Session, fmp: MagicMock
) -> None:
    # A TRANSIENT adjusted-endpoint error (429/5xx) fails the symbol rather than
    # storing a bar with a missing adj_close — it self-heals next run (vs 402,
    # which soft-fails to [] inside get_adjusted_prices; see test_fmp.py).
    fmp.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 10.5, "volume": 100},
    ]
    fmp.get_adjusted_prices.side_effect = FMPError("rate limited", status_code=429)
    result = refresh_quotes(session, fmp, ["AAPL"])
    session.commit()

    assert result.succeeded == []
    assert [f["symbol"] for f in result.failed] == ["AAPL"]
    assert session.scalar(select(DailyPrice).where(DailyPrice.symbol == "AAPL")) is None


# ----- _as_datetime helper --------------------------------------------------


def test_as_datetime_parses_common_fmp_format() -> None:
    dt = _as_datetime("2024-11-01 17:23:54")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 11 and dt.day == 1
    assert dt.hour == 17 and dt.minute == 23 and dt.second == 54
    assert dt.tzinfo is not None  # UTC-tagged


def test_as_datetime_parses_iso_format() -> None:
    dt = _as_datetime("2024-11-01T17:23:54")
    assert dt is not None and dt.year == 2024


def test_as_datetime_parses_date_only() -> None:
    dt = _as_datetime("2024-11-01")
    assert dt is not None and dt.hour == 0 and dt.day == 1


def test_as_datetime_returns_none_on_garbage() -> None:
    assert _as_datetime(None) is None
    assert _as_datetime("") is None
    assert _as_datetime("not-a-date") is None
    assert _as_datetime("   ") is None


# ----- financials -----------------------------------------------------------


def _income_payload(date_str: str, period: str, **extra) -> dict:
    return {
        "symbol": "AAPL",
        "date": date_str,
        "period": period,
        "acceptedDate": "2024-11-01 17:23:54",
        "calendarYear": "2024",
        "reportedCurrency": "USD",
        "revenue": 100.0,
        "grossProfit": 40.0,
        "operatingIncome": 25.0,
        "netIncome": 20.0,
        "eps": 1.5,
        "epsdiluted": 1.45,
        **extra,
    }


def _balance_payload(date_str: str, period: str, **extra) -> dict:
    return {
        "symbol": "AAPL",
        "date": date_str,
        "period": period,
        "acceptedDate": "2024-11-01 17:23:54",
        "totalAssets": 500.0,
        "totalLiabilities": 300.0,
        "totalEquity": 200.0,
        **extra,
    }


def _cashflow_payload(date_str: str, period: str, **extra) -> dict:
    return {
        "symbol": "AAPL",
        "date": date_str,
        "period": period,
        "acceptedDate": "2024-11-01 17:23:54",
        "operatingCashFlow": 30.0,
        "freeCashFlow": 25.0,
        "capitalExpenditure": -5.0,
        **extra,
    }


def _wire_financials_happy(fmp: MagicMock) -> None:
    fmp.get_income_statement.return_value = [_income_payload("2024-09-30", "FY")]
    fmp.get_balance_sheet.return_value = [_balance_payload("2024-09-30", "FY")]
    fmp.get_cash_flow.return_value = [_cashflow_payload("2024-09-30", "FY")]


def test_refresh_financials_known_at_equals_accepted_date(
    session: Session, fmp: MagicMock
) -> None:
    _wire_financials_happy(fmp)
    refresh_financials(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    row = session.get(IncomeStatement, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    # Critical D6 check: known_at must be the parsed acceptedDate, NOT now().
    expected = datetime(2024, 11, 1, 17, 23, 54)
    assert _naive_utc(row.known_at) == expected
    # And it must absolutely not be close to "now" (different from ingestion).
    diff = abs(_naive_utc(row.known_at) - _naive_utc(datetime.now(UTC)))
    assert diff.total_seconds() > 86400


def test_refresh_financials_inserts_all_three_tables(
    session: Session, fmp: MagicMock
) -> None:
    _wire_financials_happy(fmp)
    result = refresh_financials(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    assert result.total_upserted == 3  # one row per table
    assert result.succeeded == ["AAPL"]
    assert session.get(IncomeStatement, ("AAPL", "FY", date(2024, 9, 30))) is not None
    assert session.get(BalanceSheet, ("AAPL", "FY", date(2024, 9, 30))) is not None
    assert session.get(CashFlow, ("AAPL", "FY", date(2024, 9, 30))) is not None


def test_refresh_financials_dedupes_by_pk(session: Session, fmp: MagicMock) -> None:
    _wire_financials_happy(fmp)
    refresh_financials(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    _wire_financials_happy(fmp)
    result = refresh_financials(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    assert result.total_upserted == 0
    assert result.total_skipped == 3  # one row per table, all already present


def test_refresh_financials_both_periods_distinct_rows(
    session: Session, fmp: MagicMock
) -> None:
    # FY 2024-09-30 and Q4 2024-09-30 share fiscal_date — period discriminator
    # must keep them as separate rows.
    def income(symbol: str, *, period: str) -> list[dict]:
        return [_income_payload("2024-09-30", "FY" if period == "annual" else "Q4")]

    fmp.get_income_statement.side_effect = income
    fmp.get_balance_sheet.side_effect = lambda symbol, *, period: [
        _balance_payload("2024-09-30", "FY" if period == "annual" else "Q4")
    ]
    fmp.get_cash_flow.side_effect = lambda symbol, *, period: [
        _cashflow_payload("2024-09-30", "FY" if period == "annual" else "Q4")
    ]

    result = refresh_financials(session, fmp, ["AAPL"])
    session.commit()

    assert result.total_upserted == 6  # 3 tables × 2 periods
    rows = list(session.scalars(select(IncomeStatement.period)))
    assert sorted(rows) == ["FY", "Q4"]


def test_refresh_financials_isolates_per_period_errors(
    session: Session, fmp: MagicMock
) -> None:
    def income(symbol: str, *, period: str) -> list[dict]:
        if period == "quarter":
            raise FMPError("simulated 500", status_code=500)
        return [_income_payload("2024-09-30", "FY")]

    fmp.get_income_statement.side_effect = income
    fmp.get_balance_sheet.return_value = [_balance_payload("2024-09-30", "FY")]
    fmp.get_cash_flow.return_value = [_cashflow_payload("2024-09-30", "FY")]

    result = refresh_financials(session, fmp, ["AAPL"])
    session.commit()

    # quarter period failed, annual landed → ok=False, partial upsert recorded.
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert "quarter" in result.failed[0]["error"]
    # Annual rows still inserted.
    assert session.get(IncomeStatement, ("AAPL", "FY", date(2024, 9, 30))) is not None


def test_refresh_financials_skips_row_without_accepted_date(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_income_statement.return_value = [
        _income_payload("2024-09-30", "FY", acceptedDate=None)
    ]
    fmp.get_balance_sheet.return_value = []
    fmp.get_cash_flow.return_value = []
    result = refresh_financials(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    assert result.total_upserted == 0  # skipped due to missing known_at


def test_refresh_financials_isolates_per_symbol(session: Session, fmp: MagicMock) -> None:
    def income(symbol: str, *, period: str) -> list[dict]:
        if symbol == "BAD":
            raise FMPError("not found", status_code=404)
        return [_income_payload("2024-09-30", "FY")]

    fmp.get_income_statement.side_effect = income
    fmp.get_balance_sheet.return_value = [_balance_payload("2024-09-30", "FY")]
    fmp.get_cash_flow.return_value = [_cashflow_payload("2024-09-30", "FY")]

    result = refresh_financials(session, fmp, ["AAPL", "BAD"], periods=("annual",))
    session.commit()
    assert sorted(result.succeeded) == ["AAPL"]
    assert [f["symbol"] for f in result.failed] == ["BAD"]


# ----- ratios ---------------------------------------------------------------


def test_refresh_ratios_known_at_is_now(session: Session, fmp: MagicMock) -> None:
    fmp.get_ratios.return_value = [
        {
            "symbol": "AAPL",
            "date": "2024-09-30",
            "period": "FY",
            "priceEarningsRatio": 28.5,
            "priceToBookRatio": 40.0,
            "debtEquityRatio": 1.5,
            "returnOnEquity": 1.2,
            "grossProfitMargin": 0.45,
            "freeCashFlowYield": 0.04,
        }
    ]
    before = _naive_utc(datetime.now(UTC))
    refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()
    after = _naive_utc(datetime.now(UTC))

    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert before <= _naive_utc(row.known_at) <= after  # ingestion time, not acceptedDate
    assert row.pe_ratio == 28.5
    assert row.debt_to_equity == 1.5
    assert row.fcf_yield == 0.04


def test_refresh_ratios_handles_alt_field_names(session: Session, fmp: MagicMock) -> None:
    # FMP sometimes uses alternate names — make sure we don't drop the row.
    fmp.get_ratios.return_value = [
        {
            "symbol": "AAPL",
            "date": "2024-09-30",
            "period": "FY",
            "peRatio": 28.5,  # alt name
            "pbRatio": 40.0,  # alt name
            "debtToEquity": 1.5,  # alt name
        }
    ]
    refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert row.pe_ratio == 28.5
    assert row.price_to_book == 40.0
    assert row.debt_to_equity == 1.5


def test_refresh_ratios_merges_key_metrics(session: Session, fmp: MagicMock) -> None:
    # MA-5: /ratios omits ROE/ROA/fcf_yield — they live in /key-metrics and get
    # joined back onto the ratio row by fiscal_date.
    fmp.get_ratios.return_value = [
        {
            "symbol": "AAPL",
            "date": "2024-09-30",
            "period": "FY",
            "priceToEarningsRatio": 28.5,
        }
    ]
    fmp.get_key_metrics.return_value = [
        {
            "symbol": "AAPL",
            "date": "2024-09-30",
            "period": "FY",
            "returnOnEquity": 1.5,
            "returnOnAssets": 0.3,
            "returnOnInvestedCapital": 0.45,
            "freeCashFlowYield": 0.04,
            "earningsYield": 0.035,
        }
    ]
    refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert row.pe_ratio == 28.5  # from /ratios
    assert row.return_on_equity == 1.5  # backfilled from /key-metrics
    assert row.return_on_assets == 0.3
    assert row.return_on_invested_capital == 0.45
    assert row.fcf_yield == 0.04
    assert row.earnings_yield == 0.035


def test_refresh_ratios_keeps_ratios_value_over_key_metrics(
    session: Session, fmp: MagicMock
) -> None:
    # Defensive priority: if /ratios ever returns ROE itself, that value wins
    # and /key-metrics must not clobber it.
    fmp.get_ratios.return_value = [
        {"symbol": "AAPL", "date": "2024-09-30", "period": "FY", "returnOnEquity": 1.2}
    ]
    fmp.get_key_metrics.return_value = [
        {"symbol": "AAPL", "date": "2024-09-30", "period": "FY", "returnOnEquity": 9.9}
    ]
    refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert row.return_on_equity == 1.2


def test_refresh_ratios_key_metrics_402_marks_symbol_failed(
    session: Session, fmp: MagicMock
) -> None:
    # /key-metrics is first-class for MB screening, so a 402 (plan doesn't
    # include it) is a hard per-period failure — not a soft skip like news.
    fmp.get_ratios.return_value = [
        {
            "symbol": "AAPL",
            "date": "2024-09-30",
            "period": "FY",
            "priceToEarningsRatio": 28.5,
        }
    ]
    fmp.get_key_metrics.side_effect = FMPError("payment required", status_code=402)

    result = refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    assert result.succeeded == []
    assert len(result.failed) == 1
    assert "key-metrics" in result.failed[0]["error"]
    # The /ratios row still lands (per-period isolation); only ROE/ROA are None.
    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert row.pe_ratio == 28.5
    assert row.return_on_equity is None


# ----- analyst estimates ----------------------------------------------------


def test_refresh_estimates_inserts_new_row(session: Session, fmp: MagicMock) -> None:
    fmp.get_analyst_estimates.return_value = [
        {
            "symbol": "AAPL",
            "date": "2025-09-30",
            "period": "FY",
            "estimatedRevenueAvg": 400.0,
            "estimatedEpsAvg": 7.0,
            "numberAnalystEstimatedRevenue": 12,
        }
    ]
    result = refresh_estimates(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    assert result.total_upserted == 1
    row = session.get(AnalystEstimate, ("AAPL", date(2025, 9, 30), "FY"))
    assert row is not None
    assert row.revenue_avg == 400.0
    assert row.eps_avg == 7.0
    assert row.num_analysts_revenue == 12


def test_refresh_estimates_merge_revises_existing_row(
    session: Session, fmp: MagicMock
) -> None:
    fmp.get_analyst_estimates.return_value = [
        {"symbol": "AAPL", "date": "2025-09-30", "period": "FY", "estimatedEpsAvg": 7.0}
    ]
    refresh_estimates(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    # Force on the second call — the just-inserted row is < 7d old (fresh) so
    # the default `only_stale=True` would correctly skip it. This test
    # exercises the merge/revision semantic itself.
    fmp.get_analyst_estimates.return_value = [
        {"symbol": "AAPL", "date": "2025-09-30", "period": "FY", "estimatedEpsAvg": 7.5}
    ]
    refresh_estimates(session, fmp, ["AAPL"], periods=("annual",), only_stale=False)
    session.commit()

    row = session.get(AnalystEstimate, ("AAPL", date(2025, 9, 30), "FY"))
    assert row is not None
    assert row.eps_avg == 7.5  # revised


# ----- MA-4: only_stale default -------------------------------------------


def test_refresh_default_skips_fresh_symbols(session: Session, fmp: MagicMock) -> None:
    # Pre-seed a Profile row that's < 30d old → fresh by default threshold.
    session.add(
        Profile(symbol="AAPL", known_at=datetime.now(UTC) - timedelta(days=5), raw={})
    )
    session.commit()

    result = refresh_profile(session, fmp, ["AAPL"])
    # FMP never called: stale_symbols filtered AAPL out before the loop.
    fmp.get_profile.assert_not_called()
    assert result.outcomes == []
    assert result.total_upserted == 0


def test_refresh_force_ignores_freshness(session: Session, fmp: MagicMock) -> None:
    # Same fresh Profile pre-seed; explicit only_stale=False should hit FMP.
    session.add(
        Profile(symbol="AAPL", known_at=datetime.now(UTC) - timedelta(days=5), raw={})
    )
    session.commit()
    fmp.get_profile.return_value = {"symbol": "AAPL", "sector": "Forced"}

    refresh_profile(session, fmp, ["AAPL"], only_stale=False)
    fmp.get_profile.assert_called_once_with("AAPL")


def test_refresh_quotes_only_stale_skips_recent(session: Session, fmp: MagicMock) -> None:
    # Pre-seed bars within the 3-day window → fresh.
    today = date.today()
    session.execute(
        insert(DailyPrice),
        [{"symbol": "AAPL", "trade_date": today - timedelta(days=1), "close": 1.0}],
    )
    session.commit()

    refresh_quotes(session, fmp, ["AAPL"])
    fmp.get_historical_prices.assert_not_called()


def test_refresh_estimates_only_stale_filters(session: Session, fmp: MagicMock) -> None:
    session.add(
        AnalystEstimate(
            symbol="AAPL",
            fiscal_date=date(2025, 12, 31),
            period="FY",
            known_at=datetime.now(UTC) - timedelta(days=2),
            raw={},
        )
    )
    session.commit()

    refresh_estimates(session, fmp, ["AAPL"], periods=("annual",))
    fmp.get_analyst_estimates.assert_not_called()


def test_refresh_estimates_period_fallback_from_request(
    session: Session, fmp: MagicMock
) -> None:
    # FMP /analyst-estimates may omit `period` per row — we stamp from the
    # request param.
    fmp.get_analyst_estimates.return_value = [
        {"symbol": "AAPL", "date": "2025-12-31", "estimatedEpsAvg": 1.8}
    ]
    refresh_estimates(session, fmp, ["AAPL"], periods=("quarter",))
    session.commit()

    rows = list(session.scalars(select(AnalystEstimate)))
    assert len(rows) == 1
    assert rows[0].period == "Q"  # request_period fallback


# ----- transcript (Alpha Vantage) ------------------------------------------

_TODAY = date(2026, 5, 25)  # → walk quarters 2026Q2, 2026Q1, 2025Q4, 2025Q3


def _av_payload(symbol: str, quarter: str, content: str = "call body") -> dict:
    return {
        "symbol": symbol,
        "quarter": quarter,
        "transcript": [
            {"speaker": "Operator", "title": "Operator", "content": content, "sentiment": "0.0"}
        ],
    }


def test_refresh_transcript_inserts_latest(session: Session, av: MagicMock) -> None:
    # Any quarter has data → the newest in the walk (2026Q2) wins.
    av.get_earnings_transcript.return_value = _av_payload("NVDA", "2026Q2")
    result = refresh_transcript(session, av, ["NVDA"], only_stale=False, today=_TODAY)
    session.commit()

    assert result.total_upserted == 1
    av.get_earnings_transcript.assert_called_once_with("NVDA", quarter="2026Q2")
    row = session.get(Transcript, ("NVDA", 2026, 2))
    assert row is not None
    assert row.call_date == date(2026, 6, 30)  # quarter-end derived (AV omits date)
    assert "call body" in row.content
    assert row.known_at is not None


def test_refresh_transcript_walks_back_to_older_quarter(
    session: Session, av: MagicMock
) -> None:
    # Newest two quarters empty, the third has data → that one is stored.
    def side_effect(symbol: str, *, quarter: str):
        return _av_payload(symbol, quarter) if quarter == "2025Q4" else None

    av.get_earnings_transcript.side_effect = side_effect
    result = refresh_transcript(session, av, ["NVDA"], only_stale=False, today=_TODAY)
    session.commit()

    assert result.total_upserted == 1
    assert session.get(Transcript, ("NVDA", 2025, 4)) is not None
    assert av.get_earnings_transcript.call_count == 3  # 2026Q2, 2026Q1, 2025Q4


def test_refresh_transcript_all_empty_soft_skips(session: Session, av: MagicMock) -> None:
    av.get_earnings_transcript.return_value = None  # no quarter has data
    result = refresh_transcript(session, av, ["NVDA"], only_stale=False, today=_TODAY)
    session.commit()

    assert result.succeeded == ["NVDA"]
    assert result.total_upserted == 0
    assert result.total_skipped == 1
    assert result.failed == []
    assert list(session.scalars(select(Transcript))) == []


def test_refresh_transcript_merge_overwrites_pk(session: Session, av: MagicMock) -> None:
    av.get_earnings_transcript.return_value = _av_payload("NVDA", "2026Q2", "first")
    refresh_transcript(session, av, ["NVDA"], only_stale=False, today=_TODAY)
    session.commit()

    av.get_earnings_transcript.return_value = _av_payload("NVDA", "2026Q2", "revised")
    refresh_transcript(session, av, ["NVDA"], only_stale=False, today=_TODAY)
    session.commit()

    rows = list(session.scalars(select(Transcript)))
    assert len(rows) == 1
    assert "revised" in rows[0].content


def test_refresh_transcript_isolates_per_symbol_errors(
    session: Session, av: MagicMock
) -> None:
    def side_effect(symbol: str, *, quarter: str):
        if symbol == "BAD":
            raise AlphaVantageError("boom")
        return _av_payload(symbol, quarter)

    av.get_earnings_transcript.side_effect = side_effect
    result = refresh_transcript(
        session, av, ["NVDA", "BAD", "MSFT"], only_stale=False, today=_TODAY
    )
    session.commit()

    assert sorted(result.succeeded) == ["MSFT", "NVDA"]
    assert [f["symbol"] for f in result.failed] == ["BAD"]
    assert session.get(Transcript, ("NVDA", 2026, 2)) is not None


def test_refresh_transcript_only_stale_skips_fresh(
    session: Session, av: MagicMock
) -> None:
    # A transcript with a recent call_date is fresh (<100d) → no AV call.
    session.add(
        Transcript(
            symbol="NVDA", year=2026, quarter=1,
            call_date=date.today() - timedelta(days=5),
        )
    )
    session.commit()

    result = refresh_transcript(session, av, ["NVDA"])  # default only_stale=True
    av.get_earnings_transcript.assert_not_called()
    assert result.outcomes == []
