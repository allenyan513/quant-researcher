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

from typing import Literal

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
