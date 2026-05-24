# CLAUDE.md

> Engineering handbook for Claude Code (or any Claude-style AI collaborator).
> **Read this before touching the code.** The user-facing quick start is in
> [`README.md`](README.md); design decisions live in [`docs/`](docs/).

## Project map (read first)

Authoritative docs — consult before changing code:

- [`docs/features.md`](docs/features.md) — requirements v1.0, **decision log
  D1–D12**. Requirement changed → add a new D here.
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — implementation
  v1.0, **I1–I8 + milestones M0–MH**. Implementation strategy changed → edit here.

Code status: **M0 + MA (incl. MA-5) + MB + MC + ME (holdings) + MD + MF + MH +
MG all built** (v1: all eight capability domains closed). **MA-5** wires
`/key-metrics` to backfill ROE/ROA/fcf_yield (end of §6); **MH** is a wholesale
port of quant-engine (§13); `qr morningcall` (§14) + `qr earnings` (§15);
`financial_ratios` gained `roic` / `earnings_yield`; the backtest registry holds
6 single-symbol strategies. **MG** (signal research): `quant_researcher/signals/`
(factor registry + panel + engine) + `qr signal research/factors/list/runs/show`,
computing factor IC / quantiles / decay, persisted to `signals` / `signal_runs`
(see §16).

## Commands (check before running)

```bash
uv sync                          # deps
uv run ruff check .              # lint (must be clean)
uv run ruff check --fix .        # autofix import ordering etc.
uv run pytest -q                 # tests (in-memory SQLite, no real DB / FMP)
uv run pytest tests/test_X.py    # single file
uv run pytest -k pattern         # filter by name
```

**Before any PR**: `uv run ruff check . && uv run pytest -q` both green. CI runs
these two as well.

## Core contracts (violating one = bug)

### 1. JSON envelope: **exactly one per command**

Every `qr` subcommand emits **one** envelope to stdout via
[`quant_researcher/contract.py`](quant_researcher/contract.py)'s `Envelope`,
exit code 0=ok / 1=error. Lock-in tests: `tests/test_cli.py::test_*_single_envelope*`.

### 2. `_emit` inside a `try` double-emits the envelope ⚠

`_emit(envelope)` internally `raise typer.Exit(code)` — and `typer.Exit` is a
subclass of `Exception`. Therefore:

```python
# ❌ Wrong. typer.Exit is caught by the outer except, which emits a second
# failure envelope.
try:
    if bad:
        _emit(Envelope.failure(...))   # raises typer.Exit
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))       # fires → double envelope

# ✅ Right. Validation emits OUTSIDE the try; the try only wraps code that can
# actually throw business exceptions.
if bad:
    _emit(Envelope.failure(...))
try:
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))
else:
    _emit(Envelope.success(...))
```

MA-1 hit this (fixed in MA-2 commit `39aeb44`). New CLI commands must follow the
same shape.

### 3. Lazy-import heavy modules inside subcommands

`--help` must not trigger DB / FMP module loading. Pattern:

```python
@data_app.command("refresh")
def data_refresh(...) -> None:
    from quant_researcher.data.fmp import FMPClient        # lazy
    from quant_researcher.data.refresh import refresh_X    # lazy
    from quant_researcher.db import session_factory        # lazy
    ...
```

### 4. SQLAlchemy model registration = side-effect import

`Base.metadata` only sees model classes that have been imported. At the bottom
of `quant_researcher/db.py`:

```python
# `Base` is already defined. The line below pulls the model classes in.
from quant_researcher import models  # noqa: E402, F401
```

New model → `quant_researcher/models/X.py` → `from ... import X` in
`quant_researcher/models/__init__.py` and add it to `__all__`. **Don't forget
`__init__.py`**, or `qr db init` won't see your table.

### 5. `known_at` semantics are split (D6)

| Table | `known_at` source | `server_default` |
|---|---|---|
| `securities`, `universe`, `profiles`, `daily_prices` | `func.now()` (ingestion ≈ public time, good enough) | ✅ |
| `income_statement`, `balance_sheet`, `cash_flow` | **parsed from FMP `acceptedDate`** — strict per D6 | ❌ (set in code) |
| `financial_ratios`, `analyst_estimates` | `datetime.now(UTC)` (endpoint gives no acceptedDate) | ❌ (code uses `now(UTC)`) |

Test lock: `tests/test_models.py::test_ma3_known_at_has_no_server_default`.
**Don't add `server_default=func.now()` to the three statement tables** — it
breaks point-in-time queries.

### 6. Staleness thresholds (MA-4) + refresh defaults to only-stale

Thresholds live in [`quant_researcher/data/freshness.py`](quant_researcher/data/freshness.py)'s
`SCOPE_THRESHOLDS`, single source of truth:

| Scope | Threshold | Field judged |
|---|---|---|
| `profile` | 30 days | `MAX(known_at)` |
| `quote` | 3 calendar days | `MAX(trade_date)` — pragmatic Fri→Mon safe, no trading calendar |
| `financials` | 100 days | `MAX(fiscal_date)` from `income_statement` — "has a new quarter landed", not "recently refreshed" |
| `ratios` | 100 days | `MAX(known_at)` |
| `estimates` | 7 days | `MAX(known_at)` |

The "is it stale" logic flows through only two functions: `check_freshness(session,
symbols)` (for reports) and `stale_symbols(session, scope, symbols)` (for
filtering). **Don't duplicate thresholds or reimplement staleness queries** —
every path must go through these two.

`refresh_X(session, client, symbols, *, only_stale=True)` — `only_stale=True`
(default) is equivalent to running `symbols = stale_symbols(session, "<scope>",
symbols)` at the top of the function. The CLI `qr data refresh` follows this path
when `--force` is absent; with `--force` the CLI uses `targets` to skip the filter
and explicitly passes `only_stale=False` (avoiding a redundant filter pass).

**MA-5: `refresh_ratios` calls two endpoints.** `ROE / ROA / fcf_yield` aren't in
FMP `/ratios` (almost always None); they live in `/key-metrics`. So
`refresh_ratios` fetches `/ratios` **and** `/key-metrics` per period, joins via
`_key_metrics_by_date` on `fiscal_date`, and `_merge_key_metrics` backfills those
three fields into the ratio row — **only when the `/ratios` field is None**
(defensive: if FMP ever fills it in `/ratios`, `/ratios` wins). A `/key-metrics`
failure (e.g. plan doesn't include it → 402) is a **per-period hard-fail** (symbol
`ok=False`, error prefixed `key-metrics:`), but the `/ratios` row still ingests —
consistent with §12, **not** the soft-fail that news uses, because these three are
first-class fields for MB screening. `/key-metrics` also returns
`returnOnInvestedCapital` / `earningsYield` — **columns added**
(`return_on_invested_capital` / `earnings_yield`, screen fields `roic` /
`earnings_yield`). Adding these followed the standard flow: map in
`_KEY_METRIC_FIELDS` + None placeholder in `_ratio_from_fmp` + column on
model/screen (`_merge_key_metrics` is generic, zero change) + manual ALTER on prod.
Add more `/key-metrics` fields by copying this.

### 7. MB screening — AST sandbox + named DSL

**Fundamental expressions**
([`quant_researcher/screen/expression.py`](quant_researcher/screen/expression.py))
use `ast.parse(..., mode='eval')` to parse the string into an AST, then **walk it
manually** — **never calling `eval`**. Allowed-node whitelist: `BoolOp(And|Or)` /
`UnaryOp(Not|USub|UAdd)` / `Compare` / `Name` / `Constant` / `List` / `Tuple`.
Call / Attribute / Subscript / Lambda / comprehensions are all rejected. New
fields must enter the `FIELDS` registry (also the "valid:" list in error messages).

**Technical DSL**
([`quant_researcher/screen/technical.py`](quant_researcher/screen/technical.py))
is `name[arg1,arg2]` form, comma-separated, all predicates AND-ed. The predicate
registry is `_REGISTRY` at the bottom; to add one, write a factory returning
`Predicate = Callable[[closes, volumes], bool]`. The parser handles commas nested
inside `[…]` (depth tracking).

**State loading**
([`quant_researcher/screen/engine.py`](quant_researcher/screen/engine.py)) queries
each source table once, then aggregates per symbol in Python (simplified
greatest-N-per-group). At 300 tickers × ~10 annual ratios = 3k rows, O(N) Python
is plenty. If MG+ needs more factors, move to SQL window functions and rewrite
`build_symbol_state`.

**Adding a field**: add column → add to `FIELDS` registry → write the fill logic
in `build_symbol_state` → add a test → sync docs.

### 8. MC valuation — layered models + reproducible snapshots

**Layers** ([`quant_researcher/valuation/`](quant_researcher/valuation/))
- `wacc.py` — CAPM + Bloomberg adjust (`2/3·β + 1/3`). v1 doesn't model debt
  structure (simplified to cost-of-equity); to extend, add
  `cost_of_debt` / `tax_rate` / `debt_weight` params, DCF still takes a WACC scalar.
- `helpers.py` — read-only accessors: `historical_fcf` / `net_debt` /
  `shares_outstanding` (derived from `net_income/eps_diluted`) /
  `sector_peer_median` / `earnings_growth_rate`. All return None on missing data;
  the caller decides.
- `dcf.py` — pure functions `dcf_fcff` + `sensitivity_5x5`, no DB dependency,
  unit-testable. Terminal value is Gordon only; an exit-multiple would go through a
  `terminal_method` param.
- `peg.py` / `multiples.py` — model layer, each takes a session, pulls data once,
  computes.
- `engine.py` — `value_company` is the single public entry point; CLI and future
  Python callers both go through it. Each model writes one `valuation_snapshots`
  row (JSON `assumptions` + `result` + `sensitivity`), `code_version` auto-recorded,
  replay-aligned.

**Conventions**
- When WACC ≤ terminal_growth, `dcf_fcff` raises `DCFError` (avoids Gordon
  divide-by-zero). `sensitivity_5x5` writes such grid cells as `None` rather than
  raising.
- On missing data, `value_company` doesn't raise — it returns
  `models["dcf"]["fair_value_per_share"] = None` + a `"note": "..."`, keeping the
  envelope `ok=true`. That way one ticker's bad data doesn't kill a batch.
- Assumption-override (`assumptions` dict) keys map 1:1 to `dcf_fcff` params —
  rename in both places.
- Sector medians are computed on the fly, not cached. If MG+ needs a historically
  stable sector beta, add a `sector_betas` table; v1 doesn't need it.

### 9. ME holdings — Flex API two-step + unified importer

**Flex two-step flow**
([`quant_researcher/holdings/ibkr_flex.py`](quant_researcher/holdings/ibkr_flex.py))
1. `SendRequest?t=...&q=...&v=3` → `<FlexStatementResponse>` with a `ReferenceCode`.
2. Poll `GetStatement?t=...&q=<ref>&v=3` — while not ready IBKR returns `ErrorCode
   1019` (Status=Warn, keep polling); once ready it returns `<FlexQueryResponse>`
   and we parse `<OpenPositions><OpenPosition .../></OpenPositions>`.
   `max_poll_attempts=6` × `poll_delay=8s` covers most live queries.

**Schema discovery**: a Flex Query's fields depend on what the user ticked in the
IBKR backend. ME-1 pulled once with the user's real token and confirmed
`position` / `markPrice` / `costBasisPrice` / `fifoPnlUnrealized` / `percentOfNAV`
/ `accountId` / `reportDate` are present. Changing the Flex columns is handled
automatically (the importer keeps all attrs in `raw` JSON).

**Unified importer**
([`holdings/importer.py`](quant_researcher/holdings/importer.py))
- `import_holdings(session, source="flex"|"csv"|"manual", payload, ...)`, mapping
  internally to unified `Holding` fields.
- Uses `session.merge` — same PK `(account_id, symbol, as_of_date)` overwrites
  (re-running sync the same day updates markPrice without conflict).
- A row missing a PK field goes to `result.skipped` without blocking the rest.

**CSV format**: required `account_id, symbol, quantity, as_of_date` (YYYY-MM-DD);
optional `avg_cost / mark_price / market_value / currency / asset_category / side
/ description`. Empty numeric cells become None.

**Gotchas**:
- An OPT position's `symbol` is OCC-style (e.g. `"META  260821P00530000"`, double
  space in the middle) — don't trim/split, store verbatim.
- `position` can be negative (short); `side` is written "Short" accordingly.
- Flex `reportDate` is `YYYYMMDD` (no hyphens); `_parse_flex_date` handles it.
- Never commit the token to the repo; it lives in `.env` only.

### 10. MD research bundle — bundler + news + FMP 402 soft-fail

The **bundler**
([`quant_researcher/research/bundler.py`](quant_researcher/research/bundler.py)) is
a pure DB aggregator — it doesn't call FMP, only reads the warehouse.
`build_bundle(session, symbol)` runs 9 section helpers (`_profile_section` /
`_latest_price` / `_latest_ratios` / `_recent_statements` × 3 / `_forward_estimates`
/ `_recent_valuations` / `_holdings_section` / `_recent_news`); each returns None /
[] on missing data. `bundle(...)` adds persistence to `research_bundles` on top of
build_bundle.

**FMP 402 soft-fail**
([`quant_researcher/data/fmp.py`](quant_researcher/data/fmp.py) `get_news` /
`get_earnings_transcript`): when the user's plan excludes a premium endpoint FMP
returns 402 — these two methods catch FMPError(status_code=402) and return []. The
MA-3 statement methods still raise, since they're first-class data (MD's news is
nice-to-have).

**news table dedup**
([`research/refresh.py`](quant_researcher/research/refresh.py)): the PK is
`(symbol, published_at, url)`. Before tuple comparison, `_key()` strips both sides'
tz-aware datetimes to naive UTC, because a `DateTime(timezone=True)` column read
from SQLite is naive while Postgres is aware.

**transcript_excerpt is caller-provided**: the bundler doesn't call FMP
`/earning-call-transcript` (that endpoint is large; even truncated to 2000 chars
it's several KB). `qr research bundle` v1 passes no transcript, leaving a hook. If
an earnings-read command (`qr research earnings SYM`) is built later we'll decide
whether to fetch it actively.

### 11. MF decision ledger — record / track / scorecard

**3 entry points**
([`quant_researcher/ledger/engine.py`](quant_researcher/ledger/engine.py))
- `record_decision(session, symbol, side, thesis, confidence, tags)` — writes a
  Decision row + calls `research.bundler.bundle` to snapshot the then-current
  warehouse state into research_bundles, with `bundle_id` on the Decision.
  `price_at_open` = `_price_at_or_before(symbol, opened_at)` — **not** latest_close
  (latest_close picks up a future seed bar in tests, and possibly a just-ingested
  after-hours bar in production).
- `track_decisions(session, as_of=None)` — for each Decision × 4 horizons
  (1w/1m/3m/6m), if `target_date <= as_of`, compute forward return + SPY return +
  sector return + alpha, `session.merge` into decision_tracking. **`session.merge`
  is key** — re-running the same horizon overwrites without conflict.
- `scorecard(session, group_by, horizon)` — pull Decision + tracking rows,
  aggregate in Python by group_by ∈ {confidence, sector, tag}, return sorted by
  avg_alpha desc. tag is a list → one decision enters N tag groups.

**Key design points**
- **Alpha**: `alpha = return − benchmark`; benchmark prefers the sector ETF
  (`sectors.etf_for_sector`), falling back to SPY when no match.
- **Short decisions**: with `side="sell"`, `return_pct = -(end/start - 1)`, so a
  10% price drop → +10%.
- **Price staleness window = 3 days**: `_price_near_date` returns None when there's
  no bar within ±3 days of target_date, avoiding using a month-start price as a
  month-end one (weekends + 1 holiday is enough; a longer gap is a data problem).
- **Sector ETF mapping** is a hardcoded constant
  ([sectors.py](quant_researcher/ledger/sectors.py)), lowercase match, missing →
  fall back to SPY. FMP's sector strings vary (`"Financial Services"` vs
  `"Financials"`); the map holds both.

**Note: SPY and sector ETFs are not universe members by default** — you must
manually `qr universe set` to add SPY / XLK / XLE etc. and `qr data refresh --scope
quote` so the scorecard has benchmark data. Otherwise the alpha column is None.

### 12. Per-symbol AND per-period failure isolation

`refresh_X(session, client, symbols, *, periods=...)` — when one period of one
ticker fails:
- That period's FMP error goes into `SymbolOutcome.error` (prefixed `period:`)
- Other periods / other symbols continue
- That symbol's overall `ok=False`, but already-ingested parts are **not** rolled
  back

See `refresh_financials` + `tests/test_refresh.py::test_refresh_financials_isolates_per_*`.

### 13. MH backtest — wholesale port of quant-engine + warehouse feed + persistence

**`quant_researcher/engine/` is a wholesale port of quant-engine** (verbatim,
only the `engine.*` → `quant_researcher.engine.*` import prefix changed). **Before
changing it, decide whether you want to stay re-syncable with upstream** — large
edits make future upstream syncs hard. The only minimal changes made: ①
`data/data_feed.py` drops `YFinanceFeed` (removes the yfinance dep, keeps the
`DataFeed` ABC + `CSVFeed`); ② `analytics/metrics.py` drops the
yfinance/matplotlib version reporting; ③ `engine.py`'s `_fetch_spy_benchmark` now
reads `benchmark_symbol` from the injected `data_feed` (the original used yfinance
to auto-pull SPY), plus a `verbose` flag (default True; the CLI path passes
`verbose=False` to silence prints, preserving the §1 single envelope). **Dropped**:
`export/ optimize/ data/cached_feed.py analytics/{chart,enhanced_charts,report}.py`
(charting/QC/walk-forward not in v1). The risk/margin/stop modules are **ported but
not wired into the CLI in v1** (`risk_manager=None`).

**qr-specific orchestration in `quant_researcher/backtest/`** (doesn't pollute the
engine package, re-sync friendly):
- `engine/data/warehouse_feed.py` — `WarehouseDataFeed(DataFeed).fetch()` reads
  `daily_prices` → `Bar`. **Default `adjusted=True`**: uses `factor = adj_close/close`
  to adjust the whole OHLC bar (split/dividend correct), `close = adj_close`;
  missing `adj_close` → factor=1; rows with no close are skipped. `--raw` turns it
  off. This is the only qr-specific file under engine/data/ (additive, no upstream
  conflict).
- `backtest/strategies/` — built-in strategy registry (`REGISTRY` dict; v1 has 6
  single-symbol strategies: `sma_crossover` / `buy_and_hold` / `macd_crossover` /
  `bollinger_reversion` / `rsi_reversion` / `donchian_breakout`). To add a built-in:
  drop a module + register it in `REGISTRY` (the keys also drive the CLI's "valid:"
  error list).
- `backtest/loader.py` — `--strategy-file` uses importlib to load a `BaseStrategy`
  subclass from an external `.py` (**runs locally, not sandboxed** — same trust
  level as running any local script).
- `backtest/runner.py` — `run_backtest(...)` is the single entry point (CLI +
  Python both use it). Resolve strategy (file beats registry name) → single-symbol
  strategies auto-inject `symbols[0]` → run `BacktestEngine(verbose=False)` →
  `calculate_metrics` → write one `backtest_runs` row → return an envelope-friendly
  summary (**without** the large equity_curve/trade_log fields, which go to DB and
  are fetched by `qr backtest show`).
- **Two JSON-serialization gotchas** (both handled in runner):
  `calculate_metrics` emits **numpy scalars** → `_to_jsonable` converts to native;
  `profit_factor` etc. can be **inf/nan** → converted to None (Postgres JSONB
  rejects Infinity). New fields written to `backtest_runs` JSON columns must pass
  through `_to_jsonable`/`_num`.

**Deps**: the port pulls in `scipy` (metrics' PSR/skew/kurtosis). **Tests**:
`tests/engine/` is a wholesale port of the upstream tests (235, verifying port
correctness, change imports only); qr-specific in `tests/test_warehouse_feed.py` /
`test_backtest_runner.py` / `test_backtest_cli.py`.

**Known limitations (upstream, untouched in v1)** — flagged in the PR #6 review,
**deliberately deferred** because fixing them diverges from upstream and v1 doesn't
need them (to fix for real, change quant-engine upstream first, then sync):
- **Multi-symbol bar misalignment → lookahead bias** (`engine.py` event loop): the
  loop `advance()`s each symbol independently over `range(max_bars)`, **assuming all
  symbols' bars are perfectly aligned**. If a symbol has a gap (halt/late listing)
  its bars shift left relative to others, and one `on_bar` sees prices from
  different dates. v1's built-in strategies are all single-symbol, and the warehouse
  is a shared US trading calendar (same-period symbols are naturally aligned), so it
  doesn't trigger; **multi-symbol strategies (via `--strategy-file`) must ensure
  their symbols' histories are complete**. The fix is to iterate a deduped, sorted
  union timeline.
- **STOP_LIMIT trigger state isn't persisted** (`broker.py` `_fill_stop_limit`):
  after a stop triggers, if the limit doesn't fill on that bar it just returns None
  **without converting to a LIMIT order**, so the next bar re-evaluates the trigger
  — price crossing back un-triggers it. v1 uses market orders only (`buy`/`sell`),
  so it's untouched; custom strategies using `set_stop_loss` etc. should beware.
  The fix is to mark the order triggered and convert to LIMIT.

### 14. `qr morningcall` — portfolio morning briefing (features §E)

[`quant_researcher/research/morningcall.py`](quant_researcher/research/morningcall.py)
`build_morning_call(session, *, account=None, as_of=None, news_per_holding=1)`
assembles a **lean** portfolio briefing from holdings + warehouse (**not** N full
bundles): a lean per-holding view (weight / pnl% / day change / lean ratios /
valuation upside / 1 headline / linked decision) + a portfolio layer (total market
value / total pnl / sector exposure / top-bottom movers / cash). It **reuses** the
bundler's `_latest_price` / `_latest_ratios` / `_recent_valuations` / `_recent_news`
+ `ledger.sectors.etf_for_sector`; profile/decision are batch-queried.
`save_morning_call` persists a `MorningCallSnapshot` (uuid PK, `--save` off by
default). **Honest-data conventions**: cross-currency is raw sum + note; cash that
can't be resolved → None + note; `day_change_pct` is close-to-close (daily bars
only, no overnight gap); empty holdings → ok + notes. The valuation headline uses
dcf (there's no `"all"` model_type).

### 15. `qr earnings` — earnings actual-vs-estimate + thesis (features §D)

[`quant_researcher/research/earnings.py`](quant_researcher/research/earnings.py)
`read_earnings(session, symbol, *, limit=4, transcript_excerpt=None,
decision_limit=5)` is a **pure warehouse read** (no FMP, no writes; the transcript
is fetched online by the CLI and injected, same separation as the bundler). It
joins the most recent N `IncomeStatement` actuals to `AnalystEstimate` on the
shared PK `(symbol, fiscal_date, period)`, computes EPS/revenue surprise where an
estimate exists (`abs()` denominator guards against a negative-estimate sign flip),
and only **lists** Decisions for the thesis (no scoring, Claude judges). **Key
caveat**: estimates are forward + merge-overwritten, so a past period only has one
if it was captured "while it was still forward" → historical surprise is **sparse**;
`estimate_available` / `estimates_matched` make coverage explicit and never imply a
beat/miss when there's no estimate. `--transcript` fetches online (402-safe).

### 16. MG signal research — factor IC / quantiles / decay (features §G)

[`quant_researcher/signals/`](quant_researcher/signals/) has three layers:
`factors.py` (the `REGISTRY` factor registry) + `panel.py` (warehouse I/O + the
point-in-time panel) + `engine.py` (`run_signal` single entry point + IC/quantile/
decay math + persistence). `qr signal research --factor <name>` ranks the whole
universe by a factor on monthly rebalance dates and measures its power to predict
**forward returns**.

- **Factor registry** (`factors.py`): `fundamental` reuses `screen.expression.FIELDS`
  (factor name → financial_ratios column) + `price` (`momentum_12_1/6_1`,
  `reversal_1m`, `realized_vol_3m`, computed from a `PriceSeries`). `direction`
  (±1/0) is used only to align long-short for reporting, **never** to flip raw IC.
- **Point-in-time (PIT) correctness is everything** (`panel.py`): fundamental
  values go through a `FinancialRatios → IncomeStatement` join filtered by
  `IncomeStatement.known_at` (= the real acceptedDate) `<= rebalance_date` — **not**
  `FinancialRatios.known_at` (which is ingestion time and leaks the future). Price
  factors use only bars `<= anchor`. Forward returns use calendar-day
  `HORIZON_DAYS`; momentum uses trading-day row offsets (252/126/21).
- **Efficiency**: `load_price_panel` loads each ticker's price series into a numpy
  `PriceSeries` in one query (`adj_close`, 3-day staleness); forward-return/momentum
  are then in-memory bisects, no per-(symbol,date) queries.
- **Math** (`engine.py`, everything passing through `backtest.runner._to_jsonable`
  to guard numpy/inf/nan): IC = daily `scipy.stats.spearmanr` (**strip None/NaN
  pairs first**, constant input → nan → drop that day); summary emits mean/std/IR/
  t-stat/hit-rate; quantiles via `argsort` + `array_split` into equal buckets →
  bucket-mean returns + long-short spread (raw + direction-aligned) + monotonicity;
  decay = mean IC per horizon.
- **Honest coverage block** (always present): 2 years of prices + ~2 annual reports
  per ticker → fundamental factors are **quasi-static** (few distinct
  cross-sections, autocorrelated IC, inflated t-stats). `coverage.warnings` warns
  explicitly when a fundamental factor is quasi-static / n_dates<6 / avg_symbols<10
  — **never inflate IC on a thin sample**. The CLI emits `coverage` verbatim.
- **Persistence**: `Signal` (definition) + `SignalRun` (run_id uuid +
  ic_summary/quantiles/decay/coverage JSON), mirroring Screen/ScreenRun.

## File map

```
quant_researcher/
├── cli.py             all qr subcommands. lazy import; _emit must be OUTSIDE the try.
├── config.py          pydantic-settings; DSN scheme auto-normalized
├── contract.py        Envelope; bumping the schema means bumping SCHEMA_VERSION
├── db.py              Base + engine + session_factory + the models side-effect import at the bottom
├── universe.py        parse_watchlist_file (pure) + replace_universe (txn)
├── data/
│   ├── fmp.py         FMPClient (_get has rate-limit + retry); new endpoints go through _get_period_list
│   └── refresh.py     refresh_X functions + shared RefreshResult/SymbolOutcome + _as_* parse helpers
├── engine/            MH: wholesale quant-engine port (core/data/execution/risk/indicators/
│                      strategy/analytics/engine.py). Read §13 before changing it. warehouse_feed.py is
│                      the only qr-specific new file
├── backtest/          MH: qr orchestration layer (runner.py single entry + strategies/ registry + loader.py)
├── research/          bundler.py (research bundle, §10) + morningcall.py (§14) + earnings.py (§15)
├── signals/           MG: factors.py (registry) + panel.py (PIT panel) + engine.py (IC/quantile/decay, §16)
└── models/            one/several models per file; __init__.py re-export = registration
tests/                 mirror the structure above; in-memory SQLite + MagicMock(spec=FMPClient)
docs/                  features.md + implementation-plan.md (change design here first)
config/watchlist.txt   .gitignored (fill in on your machine); .sample is the template
.env.example           env vars needed to run; .env is .gitignored
```

## Testing conventions

- **DB**: in-memory SQLite. Fixture pattern:
  ```python
  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite://", future=True)
      Base.metadata.create_all(engine)
      with Session(engine, future=True) as sess:
          yield sess
  ```
  CLI tests use the `memory_db` fixture to patch `session_factory` to SQLite.
- **FMP business tests** (`test_refresh.py`): `MagicMock(spec=FMPClient)`, set
  `return_value` / `side_effect` per method.
- **FMP HTTP tests** (`test_fmp.py`): `respx.mock` + `httpx.Response(...)`.
- **TZ gotcha**: SQLite doesn't store tz. To compare a `DateTime(timezone=True)`
  column, normalize with the `_naive_utc(dt)` helper (see `tests/test_refresh.py`).
- **CLI tests**: `from typer.testing import CliRunner` + `_json_lines(output)` to
  parse multi-line envelopes, **asserting exactly 1**.
- **ruff `B008`** is already ignored (typer Option defaults are a documented
  pattern); don't change it back.

## Workflow for adding a feature / milestone (validated by MA-1/2/3)

1. **Docs first**: expand the milestone into subtasks in `implementation-plan.md`;
   a new requirement → add a D number in `features.md`.
2. **Branch** `<milestone>` (e.g. `ma-4`) off master.
3. **TaskCreate** the subtasks; move each `in_progress → completed`.
4. **Each step**: write model / function / CLI → add tests alongside → `uv run ruff
   check . && uv run pytest -q` must be green before the next step.
5. **Key design decisions** spelled out in docstrings + test names (cf. MA-3
   `test_refresh_financials_known_at_equals_accepted_date`).
6. **Commit message** `<milestone>: <one line>`, body lists changes + test count +
   design decisions. End with `Co-Authored-By: Claude Opus 4.7 (1M context)
   <noreply@anthropic.com>`.
7. **push + `gh pr create`**, PR body in the MA-2/MA-3 format (Summary / design
   decisions / Test plan / Out of scope).
8. **User runs e2e** (real FMP + Neon), merge after it passes.

## Schema evolution (D11: no Alembic)

- **New table**: `models/X.py` + re-export in `models/__init__.py` → `qr db init`
  applies it automatically.
- **New (nullable) column**: edit the model → `qr db init` will **not** auto-ALTER
  existing tables. ALTER manually in the Neon console's SQL Editor (`ALTER TABLE X
  ADD COLUMN ...`). Example: `financial_ratios`'s `return_on_invested_capital` /
  `earnings_yield` were added this way (`ALTER TABLE financial_ratios ADD COLUMN
  return_on_invested_capital double precision; ADD COLUMN earnings_yield double
  precision;`), then backfilled with `qr data refresh --scope ratios --force`.
- **Change / drop a column**: manual SQL in the Neon console, in order: edit model
  + run tests → ALTER prod → deploy.

## Common gotchas

- **typer.Exit is an Exception**: see §2 above.
- **SQLite has no tz**: use `_naive_utc` in tests; production Postgres has no such
  problem.
- **FMP `acceptedDate` may be absent**: `_as_datetime` returns None;
  `_ingest_statement` skips rows with known_at=None (avoiding a NOT NULL violation).
- **Some FMP endpoints' `period=quarter` is paid**: a 402 → use `--periods annual`
  as a workaround; default is still `annual,quarter`.
- **`qr data refresh` defaults to only-stale (MA-4 breaking change)**: without
  `--force`, fresh rows skip the FMP call, and the envelope's
  `scopes.<scope>.skipped_fresh` lists the skipped tickers. To reproduce the
  pre-MA-3 "refresh everything" behavior, add `--force`. The `only_stale=True`
  default also lives at the `refresh_X` function layer — change both sides together.
- **`session.scalars(select(a, b, c))` returns only the first column**; for a
  multi-column tuple use `session.execute(select(a, b, c))` then `for row in result`.
- **`Base.metadata.create_all(checkfirst=True)` doesn't modify existing tables**;
  don't expect it to auto-sync with the models.
- **`pyproject.toml`'s ruff `select = ["E", "F", "W", "I", "B", "UP"]`** +
  `ignore = ["B008"]`. Don't add lint rules without discussing first.

## Collaboration conventions (meta-spec for collaborating Claudes)

- **requirements-first**: the maintainer prefers discussing requirements/approach
  before writing code. For complex changes, **ExitPlanMode for sign-off first** —
  don't jump straight to Write/Edit.
- **Communication**: match the language the maintainer writes in (they are most
  comfortable in Chinese). Code, comments, commits, and PR titles are always in
  **English** (for international readability).
- **One PR per milestone** (MA-2 / MA-3 were each ~13 files / ~1200 LOC single PRs
  and worked well). Track subtasks with TaskCreate; don't split into multiple PRs.
- **Ask before a PR**: **wait for the maintainer's e2e confirmation** before
  push / opening a PR (as in MA-1/2/3).
- **Change docs before design**: don't sneak decisions into the code.
- **Minimal diff (surgical)**: change only the lines the task requires — every
  edit should trace back to a requirement. Don't "optimize" nearby code / comments
  / formatting in passing, don't refactor what isn't broken, **match the existing
  style** (especially ported code, e.g. `engine/` aligned with upstream). Clean up
  only orphans **you** created (unused import/var); found pre-existing dead code →
  mention it, don't delete unilaterally. Unrelated changes go in **a separate PR**.
- **Simplicity first**: the least code that solves the problem; no
  beyond-requirement features, no "flexibility" or abstraction for needs that
  haven't arrived, no error handling for impossible cases. When done, ask yourself
  "would a senior engineer think this is over-engineered?".
