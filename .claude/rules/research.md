---
paths:
  - "quant_researcher/research/**"
---

# research/ — bundle (MD), morningcall, earnings

## MD research bundle — bundler + news + FMP 402 soft-fail

The **bundler** (`quant_researcher/research/bundler.py`) is a pure DB aggregator —
it doesn't call FMP, only reads the warehouse. `build_bundle(session, symbol)` runs
the section helpers (`_profile_section` / `_latest_price` / `_latest_ratios` /
`_recent_statements` × 3 / `_forward_estimates` / `_recent_valuations` /
`_holdings_section` / `_recent_news`); each returns None / [] on missing data.
`bundle(...)` adds persistence to `research_bundles` on top of build_bundle.

**Phase 1 quality/quant sections** (additive keys on the same payload): `scores`
(Piotroski F over 2 FYs + Altman **Z''**, the 4-factor cross-industry variant —
not the manufacturing Z), `quality` (ROIC−WACC spread, FCF conversion, accruals,
multi-year margin/ROIC/revenue trends), `ratio_history` (multi-year FY multiples +
latest-vs-own-history percentile), and `roic`/`earnings_yield` surfaced in
`ratios_latest_annual`. The math lives in `research/scores.py` (pure functions, no
DB, like `valuation/dcf.py`); the bundler fetches annual (`period='FY'`) rows and
feeds them. `_safe_wacc` lazy-imports `valuation.wacc` with default rf/erp, so the
ROIC−WACC spread is an approximate, default-assumption signal. Altman Z'' needs
`balance_sheet.retained_earnings` / `current_assets` / `current_liabilities` (added
for this — see `data.md`); until prod is ALTER'd + backfilled those legs are None.

**FMP 402 soft-fail** (`quant_researcher/data/fmp.py` `get_news` /
`get_earnings_transcript`): when the user's plan excludes a premium endpoint FMP
returns 402 — these two methods catch FMPError(status_code=402) and return []. The
MA-3 statement methods still raise, since they're first-class data (news is
nice-to-have).

**news table dedup** (`quant_researcher/research/refresh.py`): the PK is `(symbol,
published_at, url)`. Before tuple comparison, `_key()` strips both sides' tz-aware
datetimes to naive UTC, because a `DateTime(timezone=True)` column read from SQLite
is naive while Postgres is aware.

**transcript section (Phase 3)**: the bundler's `_transcript_section` reads the
**latest persisted** `Transcript` row (PK `(symbol, year, quarter)`, ordered desc)
→ `transcript` payload key with year / quarter / call_date + a ~2000-char excerpt;
None when absent. The old caller-injected `transcript_excerpt` param was removed —
persistence (via `qr data refresh --scope transcript`, see `data.md`) replaced the
v1 hook. The bundler stays FMP-free (pure DB read). **Note**: the *earnings* path
(`read_earnings` / `qr earnings --transcript`) still fetches a transcript online and
injects it — that's a separate live surface, unchanged by Phase 3.

## morningcall — portfolio morning briefing (features §E)

`quant_researcher/research/morningcall.py` `build_morning_call(session, *,
account=None, as_of=None, news_per_holding=1)` assembles a **lean** portfolio
briefing from holdings + warehouse (**not** N full bundles): a lean per-holding view
(weight / pnl% / day change / lean ratios / valuation upside / 1 headline / linked
decision) + a portfolio layer (total market value / total pnl / sector exposure /
top-bottom movers / cash). It **reuses** the bundler's `_latest_price` /
`_latest_ratios` / `_recent_valuations` / `_recent_news` + `ledger.sectors.etf_for_sector`;
profile/decision are batch-queried. `save_morning_call` persists a
`MorningCallSnapshot` (uuid PK, `--save` off by default). **Honest-data conventions**:
cross-currency is raw sum + note; cash that can't be resolved → None + note;
`day_change_pct` is close-to-close (daily bars only, no overnight gap); empty
holdings → ok + notes. The valuation headline uses dcf (there's no `"all"`
model_type).

## earnings — actual-vs-estimate + thesis (features §D)

`quant_researcher/research/earnings.py` `read_earnings(session, symbol, *, limit=4,
transcript_excerpt=None, decision_limit=5)` is a **pure warehouse read** (no FMP, no
writes; the transcript is fetched online by the CLI and injected, same separation as
the bundler). It joins the most recent N `IncomeStatement` actuals to
`AnalystEstimate` on the shared PK `(symbol, fiscal_date, period)`, computes
EPS/revenue surprise where an estimate exists (`abs()` denominator guards against a
negative-estimate sign flip), and only **lists** Decisions for the thesis (no
scoring, Claude judges). **Key caveat**: estimates are forward + merge-overwritten,
so a past period only has one if it was captured "while it was still forward" →
historical surprise is **sparse**; `estimate_available` / `estimates_matched` make
coverage explicit and never imply a beat/miss when there's no estimate. `--transcript`
fetches online (402-safe).

## Sector-aware report templates (issue #37, phase 1: banks)

`research/sector_classifier.py` `classify_stock_type(sector, industry)` maps a
symbol to a `StockType` literal — `"bank"` or `"general"` in phase 1. The
bundler surfaces the result as `profile.stock_type` and uses it to dispatch the
`scores` and `quality` blocks into per-template shapes (distinguished by a
`"template": "bank" | "general"` discriminator field on each block).

Bank template payload:
- `scores`: Piotroski / Altman listed in `not_applicable` with a `not_applicable_reason`.
- `quality`: `roa` · `roe` · `net_interest_margin` · `efficiency_ratio` ·
  `equity_to_assets` + revenue `trend`. `missing_fields` lists Tier-1 / NPL
  (not in FMP standard endpoints — supplement from filings if needed). NIM
  denominator is `(total_assets_curr + total_assets_prev) / 2` as a proxy
  for true earning assets; the over-estimate is documented in the
  `scores.net_interest_margin` docstring.
- Bank metrics read `netInterestIncome` / `nonInterestIncome` /
  `nonInterestExpense` from `income_statement.raw` via the bundler's
  `_extract_raw` helper — these are real FMP fields for financial issuers,
  just not promoted to typed columns.

General template payload is unchanged from before #37, except for the
additive `"template": "general"` key so downstream consumers can dispatch
without probing for `piotroski_f`.

REIT / Insurance / Utility are deferred — adding them is a one-set change to
`BANK_INDUSTRIES` / new mapping tables in `sector_classifier.py` + one more
template branch in `_scores_section` / `_quality_section`.
