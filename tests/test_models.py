"""Model registration smoke — every MA-1 table is on `Base.metadata`."""

from __future__ import annotations

from quant_researcher.db import Base


def test_metadata_includes_ma1_tables() -> None:
    tables = set(Base.metadata.tables.keys())
    assert "universe" in tables
    assert "securities" in tables


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
