"""Unit + regression tests for the shared CLI helpers `_parse_symbols` / `_iso`.

These two helpers replaced ~6 copies of an inline symbol-parsing comprehension
and ~20 copies of `x.isoformat() if x else None` scattered across `cli.py`. The
`test_*_matches_legacy_*` cases pin the refactor to the EXACT previous behavior
(same input → same output), so the consolidation is provably non-behavioral.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from quant_researcher.cli import _iso, _parse_symbols


# Reference implementations = the exact inline expressions that lived in cli.py
# before the dedup. Kept here purely as the regression oracle.
def _legacy_parse(symbols: str) -> list[str]:
    return [s.strip().upper() for s in symbols.split(",") if s.strip()]


def _legacy_iso(value):  # noqa: ANN001 — mirrors the old untyped inline form
    return value.isoformat() if value else None


# ----- _parse_symbols: boundary behavior -----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("   ", []),
        (",", []),
        (",,", []),
        (" , , ", []),
        ("aapl", ["AAPL"]),
        ("  aapl  ", ["AAPL"]),
        ("aapl,msft", ["AAPL", "MSFT"]),
        (" aapl , msft ,, nvda ", ["AAPL", "MSFT", "NVDA"]),
        ("AAPL,aapl", ["AAPL", "AAPL"]),  # MUST NOT de-duplicate (positional)
        ("brk.b", ["BRK.B"]),  # dots preserved
        ("MsFt", ["MSFT"]),  # case-folded up
    ],
)
def test_parse_symbols_boundaries(raw: str, expected: list[str]) -> None:
    assert _parse_symbols(raw) == expected


def test_parse_symbols_preserves_order_and_duplicates() -> None:
    assert _parse_symbols("c,a,b,a") == ["C", "A", "B", "A"]


@pytest.mark.parametrize(
    "raw",
    ["", "   ", ",,", "aapl", " aapl , msft ,, nvda ", "AAPL,aapl", "brk.b", "x,Y,z"],
)
def test_parse_symbols_matches_legacy(raw: str) -> None:
    assert _parse_symbols(raw) == _legacy_parse(raw)


# ----- _iso: boundary behavior ---------------------------------------------


def test_iso_none_is_none() -> None:
    assert _iso(None) is None


def test_iso_date() -> None:
    assert _iso(date(2026, 5, 24)) == "2026-05-24"


def test_iso_datetime() -> None:
    dt = datetime(2026, 5, 24, 13, 45, 30)
    assert _iso(dt) == "2026-05-24T13:45:30"


@pytest.mark.parametrize(
    "value",
    [None, date(2026, 5, 24), date(1999, 1, 1), datetime(2026, 5, 24, 9, 30)],
)
def test_iso_matches_legacy(value) -> None:  # noqa: ANN001 — duck-typed like cli
    assert _iso(value) == _legacy_iso(value)
