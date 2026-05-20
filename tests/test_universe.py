"""universe.py — file parser + replace/list against in-memory SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.securities import Security
from quant_researcher.models.universe import UniverseMember
from quant_researcher.universe import (
    list_universe,
    parse_watchlist_file,
    replace_universe,
)

# ----- File parsing (pure) -------------------------------------------------


def test_parse_basic(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("AAPL\nMSFT\nNVDA\n")
    assert parse_watchlist_file(f) == ["AAPL", "MSFT", "NVDA"]


def test_parse_strips_blanks_and_comments(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("# header comment\n\nAAPL\n  \n  MSFT  \n# inline-style\nNVDA\n")
    assert parse_watchlist_file(f) == ["AAPL", "MSFT", "NVDA"]


def test_parse_dedupes_and_uppercases(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("aapl\nAAPL\nmsft\nMSFT\nGOOGL\n")
    assert parse_watchlist_file(f) == ["AAPL", "MSFT", "GOOGL"]


def test_parse_preserves_first_occurrence_order(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("NVDA\nAAPL\nNVDA\nMSFT\nAAPL\n")
    assert parse_watchlist_file(f) == ["NVDA", "AAPL", "MSFT"]


def test_parse_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("# only comments\n\n   \n")
    assert parse_watchlist_file(f) == []


# ----- DB integration (in-memory SQLite) -----------------------------------


@pytest.fixture
def session() -> Session:
    """Throwaway in-memory SQLite session with the project schema applied."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def test_replace_universe_from_empty(session: Session) -> None:
    result = replace_universe(session, ["AAPL", "MSFT", "NVDA"], source="test")
    session.commit()

    assert result.total == 3
    assert result.added == ["AAPL", "MSFT", "NVDA"]
    assert result.removed == []
    assert result.kept == []
    assert result.new_securities == ["AAPL", "MSFT", "NVDA"]

    syms = sorted(session.scalars(select(UniverseMember.symbol)))
    assert syms == ["AAPL", "MSFT", "NVDA"]
    sec_syms = sorted(session.scalars(select(Security.symbol)))
    assert sec_syms == ["AAPL", "MSFT", "NVDA"]


def test_replace_universe_computes_diff(session: Session) -> None:
    replace_universe(session, ["AAPL", "MSFT", "NVDA"], source="v1")
    session.commit()

    result = replace_universe(session, ["AAPL", "GOOGL", "TSLA"], source="v2")
    session.commit()

    assert result.added == ["GOOGL", "TSLA"]
    assert result.removed == ["MSFT", "NVDA"]
    assert result.kept == ["AAPL"]
    # MSFT/NVDA stay in `securities` (master) — only the universe row is gone.
    sec_syms = sorted(session.scalars(select(Security.symbol)))
    assert sec_syms == ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]
    new_uni = sorted(session.scalars(select(UniverseMember.symbol)))
    assert new_uni == ["AAPL", "GOOGL", "TSLA"]


def test_replace_universe_updates_source(session: Session) -> None:
    replace_universe(session, ["AAPL"], source="v1")
    session.commit()
    replace_universe(session, ["AAPL"], source="v2")
    session.commit()
    row = session.scalars(select(UniverseMember)).one()
    assert row.source == "v2"


def test_list_universe_orders_by_symbol(session: Session) -> None:
    replace_universe(session, ["NVDA", "AAPL", "MSFT"], source="test")
    session.commit()
    rows = list_universe(session)
    assert [r.symbol for r in rows] == ["AAPL", "MSFT", "NVDA"]


def test_list_universe_respects_limit(session: Session) -> None:
    replace_universe(session, ["AAPL", "MSFT", "NVDA", "GOOGL"], source="test")
    session.commit()
    rows = list_universe(session, limit=2)
    assert [r.symbol for r in rows] == ["AAPL", "GOOGL"]
