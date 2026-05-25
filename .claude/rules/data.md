---
paths:
  - "quant_researcher/data/**"
---

# data/ — refresh, freshness, staleness (MA-4/MA-5)

## Staleness thresholds + refresh defaults to only-stale

Thresholds live in `quant_researcher/data/freshness.py`'s `SCOPE_THRESHOLDS`,
single source of truth:

| Scope | Threshold | Field judged |
|---|---|---|
| `profile` | 30 days | `MAX(known_at)` |
| `quote` | 3 calendar days | `MAX(trade_date)` — pragmatic Fri→Mon safe, no trading calendar |
| `financials` | 100 days | `MAX(fiscal_date)` from `income_statement` — "has a new quarter landed", not "recently refreshed" |
| `ratios` | 100 days | `MAX(known_at)` |
| `estimates` | 7 days | `MAX(known_at)` |
| `transcript` | 100 days | `MAX(call_date)` from `transcripts` — quarterly, judged on the call's own date (not `known_at`), like `financials` |
| `insider` | 30 days | `MAX(filing_date)` from `insider_transactions` — Form 4s land sporadically; re-check monthly |
| `short` | 25 days | `MAX(settlement_date)` from `short_interest` — FINRA bi-monthly + ~7bd publish lag (so the latest *available* settlement isn't false-flagged stale during the gap) |

**`transcript` scope (Phase 3)**: source is **Alpha Vantage**, NOT FMP — FMP gates
transcripts behind a premium tier (402), AV serves them on the free key. Its own
thin client (`data/alphavantage.py` `AlphaVantageClient`, `ALPHA_VANTAGE_API_KEY`)
and its own CLI branch (the dispatch splits: `if scope == "transcript":` opens the
AV client, `else:` the FMP client). AV's endpoint needs an explicit quarter (no
"latest"), so `refresh_transcript` **walks recent quarter labels** (`YYYY'Q'N`,
newest→oldest, `_TRANSCRIPT_LOOKBACK_QUARTERS=4`) and takes the first with data;
`session.merge` by PK `(symbol, year, quarter)`. AV omits the call date → `call_date`
is derived from the quarter end (used only for freshness); the full speaker-segmented
payload (with per-segment sentiment) is kept in `raw`, joined text in `content`.
No transcript in the window / a rate-limit reply → **soft-skip** (`ok=True,
skipped=1`); a hard `AlphaVantageError` → `ok=False` (isolated). Brand-new table →
`qr db init` auto-creates it (no ALTER). **Excluded from `--scope all`** (free tier
~25 req/day; per-name pull) — run targeted: `--scope transcript --symbols SYM`.
AV free tier is ~1 req/sec; the client self-throttles (`min_interval_s`).

**`insider` scope (Phase 5)**: source is **free SEC EDGAR** via the `edgartools`
dependency (FMP gates insider data). `data/edgar.py` `EdgarClient(identity)` sets
SEC's mandatory User-Agent (`SEC_EDGAR_IDENTITY`, "Name email") and pulls recent
Form 4 filings → flattens each `.obj().to_dataframe()` into transaction rows.
`refresh_insider` UPSERTs by PK `(symbol, accession_no, line_no)` via
`session.merge` (filings are immutable); no Form 4s in the lookback window → soft-skip
(`ok=True, skipped=1`); a hard `EdgarError` → `ok=False` (isolated). Its own CLI
branch (no FMP/AV client; `EdgarClient` is not a context manager). Brand-new table
`insider_transactions` → `qr db init` auto-creates it (no ALTER). **Excluded from
`--scope all`** (per-name, network-heavy via SEC's ≤10 req/s) — run targeted.
Note: 13F institutional ownership is deliberately deferred (it's an inverse lookup
needing a CUSIP↔ticker map + 45-day lag — a separate project; edgartools is
manager-centric).

**`short` scope (Phase 5)**: source is **free, auth-free FINRA** (`data/finra.py`
`FinraClient`, no key). FINRA publishes ONE bi-monthly CSV covering all securities
at `cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv`; the client resolves
the latest *published* file by probing recent settlement dates (15th + month-end,
weekend-nudged) newest-first (absorbing the ~7bd lag), downloads it **once**, and
returns rows for the requested symbols — so one download serves the whole batch.
`refresh_short_interest` UPSERTs by PK `(symbol, settlement_date)`; a symbol absent
from the file → soft-skip; a download/parse failure fails all requested symbols
(shared file). Its own CLI branch (`FinraClient`, **no credential** — the only
ownership scope needing no key). New table `short_interest` → `qr db init` (no
ALTER). **Excluded from `--scope all`** (own client; bi-monthly cadence).

The "is it stale" logic flows through only two functions: `check_freshness(session,
symbols)` (for reports) and `stale_symbols(session, scope, symbols)` (for
filtering). **Don't duplicate thresholds or reimplement staleness queries** —
every path must go through these two.

`refresh_X(session, client, symbols, *, only_stale=True)` — `only_stale=True`
(default) is equivalent to running `symbols = stale_symbols(session, "<scope>",
symbols)` at the top of the function. The CLI `qr data refresh` follows this path
when `--force` is absent; with `--force` the CLI uses `targets` to skip the filter
and explicitly passes `only_stale=False` (avoiding a redundant filter pass).

## MA-5: `refresh_ratios` calls two endpoints

`ROE / ROA / fcf_yield` aren't in FMP `/ratios` (almost always None); they live in
`/key-metrics`. So `refresh_ratios` fetches `/ratios` **and** `/key-metrics` per
period, joins via `_key_metrics_by_date` on `fiscal_date`, and `_merge_key_metrics`
backfills those three fields into the ratio row — **only when the `/ratios` field is
None** (defensive: if FMP ever fills it in `/ratios`, `/ratios` wins). A
`/key-metrics` failure (e.g. plan doesn't include it → 402) is a **per-period
hard-fail** (symbol `ok=False`, error prefixed `key-metrics:`), but the `/ratios`
row still ingests — consistent with the failure-isolation rule below, **not** the
soft-fail that news uses (see `research.md`), because these three are first-class
fields for MB screening. `/key-metrics` also returns `returnOnInvestedCapital` /
`earningsYield` — **columns added** (`return_on_invested_capital` / `earnings_yield`,
screen fields `roic` / `earnings_yield`). Adding these followed the standard flow:
map in `_KEY_METRIC_FIELDS` + None placeholder in `_ratio_from_fmp` + column on
model/screen (`_merge_key_metrics` is generic, zero change) + manual ALTER on prod.
Add more `/key-metrics` fields by copying this.

## Per-symbol AND per-period failure isolation

`refresh_X(session, client, symbols, *, periods=...)` — when one period of one
ticker fails:
- That period's FMP error goes into `SymbolOutcome.error` (prefixed `period:`)
- Other periods / other symbols continue
- That symbol's overall `ok=False`, but already-ingested parts are **not** rolled back

See `refresh_financials` + `tests/test_refresh.py::test_refresh_financials_isolates_per_*`.

## gotchas

- **FMP `acceptedDate` may be absent**: `_as_datetime` returns None;
  `_ingest_statement` skips rows with known_at=None (avoiding a NOT NULL violation).
- **Some FMP endpoints' `period=quarter` is paid**: a 402 → use `--periods annual`
  as a workaround; default is still `annual,quarter`.
- **`qr data refresh` defaults to only-stale (MA-4 breaking change)**: without
  `--force`, fresh rows skip the FMP call, and the envelope's
  `scopes.<scope>.skipped_fresh` lists the skipped tickers. To reproduce the
  pre-MA-3 "refresh everything" behavior, add `--force`. The `only_stale=True`
  default also lives at the `refresh_X` function layer — change both sides together.
- **New FMP endpoint**: `data/fmp.py` add method (`_get` has rate-limit + retry;
  go through `_get_period_list`) → `data/refresh.py` add `refresh_X` → `cli.py`
  extend `_VALID_SCOPES` + if-block → add tests. `MagicMock(spec=FMPClient)` for
  business tests, `respx` for HTTP-layer tests.
