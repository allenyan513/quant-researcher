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
