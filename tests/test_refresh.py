"""refresh.py — profile + quotes refresh logic with a mocked FMP client."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.data.refresh import (
    _as_datetime,
    refresh_estimates,
    refresh_financials,
    refresh_profile,
    refresh_quotes,
    refresh_ratios,
)
from quant_researcher.db import Base
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


@pytest.fixture
def fmp() -> MagicMock:
    return MagicMock(spec=FMPClient)


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
            "freeCashFlowYield": 0.04,
        }
    ]
    refresh_ratios(session, fmp, ["AAPL"], periods=("annual",))
    session.commit()

    row = session.get(FinancialRatios, ("AAPL", "FY", date(2024, 9, 30)))
    assert row is not None
    assert row.pe_ratio == 28.5  # from /ratios
    assert row.return_on_equity == 1.5  # backfilled from /key-metrics
    assert row.return_on_assets == 0.3
    assert row.fcf_yield == 0.04


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
