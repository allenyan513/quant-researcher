"""EdgarClient — pure coercion helpers + the identity guard (no network)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from quant_researcher.data.edgar import EdgarClient, EdgarError, _as_date, _f, _s


def test_as_date_variants() -> None:
    assert _as_date(date(2026, 5, 8)) == date(2026, 5, 8)
    assert _as_date(datetime(2026, 5, 8, 12, 0)) == date(2026, 5, 8)
    assert _as_date("2026-05-08") == date(2026, 5, 8)
    assert _as_date("2026-05-08 00:00:00") == date(2026, 5, 8)
    assert _as_date(None) is None
    assert _as_date("garbage") is None


def test_f_coercion() -> None:
    assert _f("1000") == 1000.0
    assert _f(50.5) == 50.5
    assert _f(None) is None
    assert _f(float("nan")) is None
    assert _f("x") is None


def test_s_coercion() -> None:
    assert _s("  CEO ") == "CEO"
    assert _s("nan") is None
    assert _s("") is None
    assert _s(None) is None


def test_missing_identity_raises() -> None:
    # The guard fires before edgartools is imported (no network).
    with pytest.raises(EdgarError):
        EdgarClient(identity="")
