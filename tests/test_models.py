"""Model registration smoke — every MA-1 table is on `Base.metadata`."""

from __future__ import annotations

from quant_researcher.db import Base


def test_metadata_includes_ma1_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "universe" in tables
    assert "securities" in tables


def test_metadata_includes_ma2_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "profiles" in tables
    assert "daily_prices" in tables


def test_universe_table_shape() -> None:
    t = Base.metadata.tables["universe"]
    cols = {c.name for c in t.columns}
    assert cols == {"symbol", "source", "added_at"}
    # symbol is the only PK
    assert [c.name for c in t.primary_key.columns] == ["symbol"]


def test_securities_table_shape() -> None:
    t = Base.metadata.tables["securities"]
    cols = {c.name for c in t.columns}
    assert cols == {"symbol", "is_active", "first_seen_at"}
    assert [c.name for c in t.primary_key.columns] == ["symbol"]


def test_profiles_table_shape() -> None:
    t = Base.metadata.tables["profiles"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "symbol",
        "company_name",
        "sector",
        "industry",
        "exchange",
        "currency",
        "country",
        "beta",
        "ipo_date",
        "is_etf",
        "is_fund",
        "is_adr",
        "is_actively_trading",
        "raw",
        "known_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["symbol"]


def test_daily_prices_table_shape() -> None:
    t = Base.metadata.tables["daily_prices"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "known_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["symbol", "trade_date"]


def test_metadata_includes_ma3_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    for name in (
        "income_statement",
        "balance_sheet",
        "cash_flow",
        "financial_ratios",
        "analyst_estimates",
    ):
        assert name in tables, f"missing {name}"


def test_income_statement_table_shape() -> None:
    t = Base.metadata.tables["income_statement"]
    cols = {c.name for c in t.columns}
    assert {
        "symbol",
        "period",
        "fiscal_date",
        "calendar_year",
        "reported_currency",
        "raw",
        "known_at",
        "revenue",
        "cost_of_revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "eps",
        "eps_diluted",
    } == cols
    assert [c.name for c in t.primary_key.columns] == ["symbol", "period", "fiscal_date"]


def test_balance_sheet_table_shape() -> None:
    t = Base.metadata.tables["balance_sheet"]
    cols = {c.name for c in t.columns}
    assert {
        "symbol",
        "period",
        "fiscal_date",
        "calendar_year",
        "reported_currency",
        "raw",
        "known_at",
        "total_assets",
        "total_liabilities",
        "total_equity",
        "cash_and_equivalents",
        "short_term_debt",
        "long_term_debt",
    } == cols
    assert [c.name for c in t.primary_key.columns] == ["symbol", "period", "fiscal_date"]


def test_cash_flow_table_shape() -> None:
    t = Base.metadata.tables["cash_flow"]
    cols = {c.name for c in t.columns}
    assert {
        "symbol",
        "period",
        "fiscal_date",
        "calendar_year",
        "reported_currency",
        "raw",
        "known_at",
        "operating_cash_flow",
        "investing_cash_flow",
        "financing_cash_flow",
        "capital_expenditure",
        "free_cash_flow",
        "dividends_paid",
    } == cols
    assert [c.name for c in t.primary_key.columns] == ["symbol", "period", "fiscal_date"]


def test_financial_ratios_table_shape() -> None:
    t = Base.metadata.tables["financial_ratios"]
    pk = [c.name for c in t.primary_key.columns]
    assert pk == ["symbol", "period", "fiscal_date"]
    cols = {c.name for c in t.columns}
    # Spot-check key metrics rather than exhaustive list (15 ratios).
    for name in (
        "pe_ratio",
        "peg_ratio",
        "ev_to_ebitda",
        "debt_to_equity",
        "return_on_equity",
        "fcf_yield",
        "raw",
        "known_at",
    ):
        assert name in cols, f"missing {name}"


def test_analyst_estimates_table_shape() -> None:
    t = Base.metadata.tables["analyst_estimates"]
    cols = {c.name for c in t.columns}
    assert {
        "symbol",
        "fiscal_date",
        "period",
        "revenue_avg",
        "revenue_low",
        "revenue_high",
        "eps_avg",
        "eps_low",
        "eps_high",
        "ebitda_avg",
        "net_income_avg",
        "num_analysts_revenue",
        "num_analysts_eps",
        "raw",
        "known_at",
    } == cols
    assert [c.name for c in t.primary_key.columns] == ["symbol", "fiscal_date", "period"]


def test_ma3_known_at_has_no_server_default() -> None:
    # D6 strict: financial statement known_at must be set by code (parsed
    # acceptedDate), never auto-populated by the DB default. Locking this in.
    for table_name in ("income_statement", "balance_sheet", "cash_flow"):
        col = Base.metadata.tables[table_name].columns["known_at"]
        assert col.server_default is None, (
            f"{table_name}.known_at must NOT have a server_default (D6)"
        )
        assert not col.nullable
