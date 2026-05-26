"""Pure unit tests on the sector → stock_type classifier (issue #37)."""

from __future__ import annotations

import pytest

from quant_researcher.research.sector_classifier import (
    BANK_INDUSTRIES,
    classify_stock_type,
    net_revenue,
)


@pytest.mark.parametrize(
    "sector,industry,expected",
    [
        # ---- bank ----
        ("Financial Services", "Banks—Regional", "bank"),
        ("Financial Services", "Banks—Diversified", "bank"),
        ("Financial Services", "Banks - Regional", "bank"),      # alt separator
        ("Financial Services", "Banks - Diversified", "bank"),   # alt separator
        ("Financial Services", "Banks", "bank"),                 # bare plural
        ("Financial Services", "Credit Services", "bank"),       # legacy SoFi-style
        # Real FMP industry strings in the warehouse (verified):
        ("Financial Services", "Financial - Credit Services", "bank"),    # AXP, COF, SOFI
        ("Financial Services", "Financial - Capital Markets", "bank"),    # GS, MS, HOOD, RJF
        ("Financial Services", "Capital Markets", "bank"),                # alt
        ("Financial Services", "Investment - Banking & Investment Services", "bank"),  # IBKR
        # Substring fallback for any future "Banks—X" we haven't enumerated.
        ("Financial Services", "Banks—Major Regional", "bank"),
        # ---- general (explicit Financial Services NON-bank industries) ----
        ("Financial Services", "Asset Management", "general"),
        ("Financial Services", "Insurance—Diversified", "general"),
        ("Financial Services", "Financial - Data & Stock Exchanges", "general"),  # CBOE, CME
        # ---- general (everything else) ----
        ("Technology", "Consumer Electronics", "general"),       # AAPL
        ("Technology", "Semiconductors", "general"),             # MU / NVDA
        ("Healthcare", "Drug Manufacturers—General", "general"),
        ("Energy", "Oil & Gas Integrated", "general"),
        # ---- None inputs ----
        (None, None, "general"),
        ("Financial Services", None, "general"),
        (None, "Banks—Regional", "bank"),  # industry alone is enough
        # ---- whitespace tolerance ----
        ("Financial Services", "  Banks—Regional  ", "bank"),
    ],
)
def test_classify_stock_type(
    sector: str | None, industry: str | None, expected: str
) -> None:
    assert classify_stock_type(sector, industry) == expected


def test_bank_industries_set_includes_separator_variants() -> None:
    # Both em-dash and hyphen variants must be present — FMP's payload
    # uses inconsistent separators across symbols.
    assert "Banks—Regional" in BANK_INDUSTRIES
    assert "Banks - Regional" in BANK_INDUSTRIES
    # Credit Services covers SoFi / Discover / AmEx.
    assert "Credit Services" in BANK_INDUSTRIES


# ----- net_revenue (issue #36) -------------------------------------------


class _FakeIncome:
    """Stand-in for an IncomeStatement ORM row in unit tests."""

    def __init__(self, revenue: float | None, raw: dict | None = None) -> None:
        self.revenue = revenue
        self.raw = raw


def test_net_revenue_bank_derives_from_interest_expense() -> None:
    inc = _FakeIncome(revenue=125e9, raw={"interestExpense": 67e9})
    # gross 125 − interestExpense 67 = 58 (net)
    assert net_revenue(inc, "bank") == 58e9


def test_net_revenue_general_returns_unchanged() -> None:
    inc = _FakeIncome(revenue=400e9, raw={"interestExpense": 50e9})
    # Non-bank: ignore interestExpense, return raw revenue (AAPL-style).
    assert net_revenue(inc, "general") == 400e9


def test_net_revenue_bank_without_interest_expense_falls_back_to_gross() -> None:
    inc = _FakeIncome(revenue=100e9, raw={})  # FMP didn't return interestExpense
    # Can't derive net; return gross rather than None so the field isn't lost.
    assert net_revenue(inc, "bank") == 100e9


def test_net_revenue_bank_with_none_revenue() -> None:
    inc = _FakeIncome(revenue=None, raw={"interestExpense": 10e9})
    assert net_revenue(inc, "bank") is None


def test_net_revenue_bank_no_raw_attribute() -> None:
    inc = _FakeIncome(revenue=100e9, raw=None)
    assert net_revenue(inc, "bank") == 100e9  # fall back to gross
