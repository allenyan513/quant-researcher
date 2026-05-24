---
paths:
  - "quant_researcher/research/**"
---

# research/ — bundle (MD), morningcall, earnings

## MD research bundle — bundler + news + FMP 402 soft-fail

The **bundler** (`quant_researcher/research/bundler.py`) is a pure DB aggregator —
it doesn't call FMP, only reads the warehouse. `build_bundle(session, symbol)` runs
9 section helpers (`_profile_section` / `_latest_price` / `_latest_ratios` /
`_recent_statements` × 3 / `_forward_estimates` / `_recent_valuations` /
`_holdings_section` / `_recent_news`); each returns None / [] on missing data.
`bundle(...)` adds persistence to `research_bundles` on top of build_bundle.

**FMP 402 soft-fail** (`quant_researcher/data/fmp.py` `get_news` /
`get_earnings_transcript`): when the user's plan excludes a premium endpoint FMP
returns 402 — these two methods catch FMPError(status_code=402) and return []. The
MA-3 statement methods still raise, since they're first-class data (news is
nice-to-have).

**news table dedup** (`quant_researcher/research/refresh.py`): the PK is `(symbol,
published_at, url)`. Before tuple comparison, `_key()` strips both sides' tz-aware
datetimes to naive UTC, because a `DateTime(timezone=True)` column read from SQLite
is naive while Postgres is aware.

**transcript_excerpt is caller-provided**: the bundler doesn't call FMP
`/earning-call-transcript` (that endpoint is large; even truncated to 2000 chars
it's several KB). `qr research bundle` v1 passes no transcript, leaving a hook.

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
