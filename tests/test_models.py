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
        "retained_earnings",
        "current_assets",
        "current_liabilities",
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


def test_metadata_includes_mb_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "screens" in tables
    assert "screen_runs" in tables


def test_screens_table_shape() -> None:
    t = Base.metadata.tables["screens"]
    cols = {c.name for c in t.columns}
    assert cols == {"name", "expr", "technical", "description", "created_at"}
    assert [c.name for c in t.primary_key.columns] == ["name"]


def test_screen_runs_table_shape() -> None:
    t = Base.metadata.tables["screen_runs"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "run_id",
        "screen_name",
        "expr",
        "technical",
        "expr_hash",
        "ran_at",
        "universe_size",
        "result_symbols",
        "code_version",
    }
    assert [c.name for c in t.primary_key.columns] == ["run_id"]


def test_metadata_includes_mc_tables() -> None:
    assert "valuation_snapshots" in set(Base.metadata.tables.keys())


def test_valuation_snapshots_table_shape() -> None:
    t = Base.metadata.tables["valuation_snapshots"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "snapshot_id",
        "symbol",
        "model_type",
        "as_of",
        "fair_value_per_share",
        "current_price",
        "upside_pct",
        "assumptions",
        "result",
        "sensitivity",
        "code_version",
        "created_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["snapshot_id"]


def test_metadata_includes_me_tables() -> None:
    assert "holdings" in set(Base.metadata.tables.keys())


def test_holdings_table_shape() -> None:
    t = Base.metadata.tables["holdings"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "account_id",
        "symbol",
        "as_of_date",
        "asset_category",
        "sub_category",
        "quantity",
        "mark_price",
        "market_value",
        "avg_cost",
        "cost_basis_total",
        "unrealized_pnl",
        "percent_of_nav",
        "side",
        "currency",
        "fx_rate_to_base",
        "conid",
        "listing_exchange",
        "description",
        "raw",
        "source",
        "known_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["account_id", "symbol", "as_of_date"]


def test_metadata_includes_md_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "news_items" in tables
    assert "research_bundles" in tables


def test_news_items_table_shape() -> None:
    t = Base.metadata.tables["news_items"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "symbol",
        "published_at",
        "url",
        "headline",
        "source",
        "summary",
        "image_url",
        "raw",
        "known_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["symbol", "published_at", "url"]


def test_research_bundles_table_shape() -> None:
    t = Base.metadata.tables["research_bundles"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "bundle_id",
        "symbol",
        "as_of",
        "payload",
        "code_version",
        "created_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["bundle_id"]


def test_metadata_includes_mf_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "decisions" in tables
    assert "decision_tracking" in tables


def test_decisions_table_shape() -> None:
    t = Base.metadata.tables["decisions"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "decision_id",
        "symbol",
        "side",
        "opened_at",
        "price_at_open",
        "thesis",
        "confidence",
        "tags",
        "sector_at_open",
        "bundle_id",
        "code_version",
        "created_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["decision_id"]


def test_decision_tracking_table_shape() -> None:
    t = Base.metadata.tables["decision_tracking"]
    cols = {c.name for c in t.columns}
    assert cols == {
        "decision_id",
        "horizon",
        "tracked_at",
        "price",
        "return_pct",
        "spy_return_pct",
        "sector_etf",
        "sector_return_pct",
        "alpha_pct",
        "extras",
        "updated_at",
    }
    assert [c.name for c in t.primary_key.columns] == ["decision_id", "horizon"]
