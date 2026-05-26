"""Stock-type classifier — drives sector-aware report templates.

Pure function, no DB (mirrors `scores.py` shape). Maps a symbol's
`(sector, industry)` from the FMP profile to a small enum used by the
bundler to dispatch its `scores` / `quality` sections, and by the
deep-research skill to fork its §4 (Financial quality) / §5 (Valuation)
template.

Phase 1 supports only **`"bank"`** vs **`"general"`**. REIT / Insurance
/ Utility are deliberately deferred — adding them is a one-set change
to the mapping tables below plus a new `StockType` literal value.

`"general"` is the default — every existing non-bank stays on the
historical path.
"""

from __future__ import annotations

from typing import Any, Literal

StockType = Literal["bank", "general"]

# FMP /profile uses several separator and prefix conventions across
# payloads — em-dash ("Banks—Regional"), hyphen-with-spaces ("Banks -
# Regional"), bare plural ("Banks"), and "Financial - X" / "Investment
# - X" forms. Enumerate the variants we've seen.
#
# What goes in: anything whose balance sheet is deposit-funded /
# inventory-heavy enough that Piotroski / Altman / FCF / ROIC-WACC lose
# meaning — commercial banks, consumer-finance lenders, investment
# banks / broker-dealers.
#
# Known false positives:
# * "Financial - Credit Services" mixes pure card issuers (AXP / COF /
#   SoFi — bank-template-appropriate) with payment networks (V / MA —
#   "general" would be better). The taxonomy doesn't disambiguate; we
#   pick the bigger-impact case (card issuers) and accept the
#   collateral on payment networks. Their bank-template output is
#   still honest — NIM ≈ 0 makes the lack-of-NII obvious.
BANK_INDUSTRIES = frozenset({
    # Commercial / consumer banks
    "Banks",
    "Banks—Regional",
    "Banks—Diversified",
    "Banks - Regional",
    "Banks - Diversified",
    # Consumer finance / card issuers (FMP nomenclature varies)
    "Credit Services",
    "Financial - Credit Services",
    # Investment banks / broker-dealers
    "Financial - Capital Markets",
    "Capital Markets",
    "Investment - Banking & Investment Services",
})


def classify_stock_type(
    sector: str | None, industry: str | None
) -> StockType:
    """Return the report template to use for this stock.

    Lookup is exact-match against `BANK_INDUSTRIES` first; then a
    sector-and-substring fallback for any future "Banks—X" industry
    label we haven't enumerated. Both inputs may be None
    (symbol has no profile yet) → `"general"`.
    """
    if industry:
        industry = industry.strip()
        if industry in BANK_INDUSTRIES:
            return "bank"
        if sector == "Financial Services" and "Bank" in industry:
            return "bank"
    return "general"


def net_revenue(income_row: Any, stock_type: StockType) -> float | None:
    """Bank-aware revenue (issue #36).

    For a bank, FMP's `/income-statement` `revenue` line is **gross**
    (interestIncome + non-interest income) — sell-side analysts and
    GAAP-style bank reporting use **net revenue** instead (gross −
    interestExpense). Comparing gross actual to net consensus produces
    the +111% / +144% revenue-surprise nonsense we saw on GS.

    For `stock_type == "bank"` this returns
    `revenue − interestExpense` derived from `income_row.raw`. For
    everything else (and when interestExpense isn't in the raw blob)
    it returns the unchanged `revenue` line — non-financials' gross
    and net revenue are the same.
    """
    rev = getattr(income_row, "revenue", None)
    if stock_type != "bank" or rev is None:
        return rev
    raw = getattr(income_row, "raw", None) or {}
    int_exp = raw.get("interestExpense")
    if int_exp is None:
        return rev  # can't derive — keep gross rather than null
    try:
        return float(rev) - float(int_exp)
    except (TypeError, ValueError):
        return rev
