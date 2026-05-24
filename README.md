# quant-researcher

> A point-in-time US-equity research warehouse and analysis toolkit that an LLM
> agent (Claude Code) drives over a stable **JSON-on-stdout CLI contract**. It
> does the unglamorous part — fetch, compute, persist, reproduce — so the agent
> can focus on judgment and narrative.

[![CI](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml/badge.svg)](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)

## Why this exists

LLM agents reason well but can't reliably *get* clean, point-in-time financial
data — or remember what they decided and how it turned out. quant-researcher is
the substrate underneath the agent:

- **Agent-first contract, not a black box.** Every `qr` subcommand prints
  **exactly one JSON envelope** on stdout. Claude chains them — screen → value →
  research bundle → record decision → track alpha — and writes the prose itself.
  The system never does NLP or narrative; that's the agent's job.
- **Point-in-time correct.** Financial statements are stamped with FMP's
  `acceptedDate`, so screens and backtests see only what was knowable on the
  as-of date. No lookahead bias.
- **Reproducible and auditable.** Every result carries `as_of`, `code_version`,
  and an optional snapshot id. A decision snapshots the exact data it was made
  on — so you can grade the agent's judgment months later.
- **Yours, low-config.** Built around a personal IBKR + CSV + Claude-skills
  workflow. Opinionated defaults over endless configuration.

## What it does — eight capability domains

| | Domain | Highlights |
|---|---|---|
| **A** | Data warehouse | FMP ingest, layered refresh, freshness-aware (only refreshes stale rows) |
| **B** | Screening | fundamental AST expressions **and** a technical-predicate DSL; saved, runnable, diffable |
| **C** | Valuation | DCF-FCFF + PEG + relative multiples, 5×5 sensitivity grid, reproducible snapshots |
| **D** | Research & earnings | one-shot research bundle; earnings actual-vs-estimate with surprise |
| **E** | Holdings & morning call | IBKR Flex **or any-broker CSV**; lean portfolio briefing |
| **F** | Decision ledger | record thesis + data snapshot, track 1w–6m alpha vs SPY / sector ETF |
| **G** | Signal research | factor IC / quantile spread / decay, strictly point-in-time |
| **H** | Backtesting | ported event-driven engine, 6 built-in strategies + external strategy files |

A single chain looks like this — and Claude orchestrates it from one natural-language ask:

```bash
qr screen run --expr "pe < 25 and fcf_yield > 0.05 and roic > 0.12"   # find candidates
qr value AAPL --model all                                              # fair value + upside
qr research bundle AAPL                                                # one JSON with everything
qr ledger add AAPL --side buy --thesis "cheap compounder" --confidence 4
qr ledger track                                                        # later: did it beat SPY?
```

## Quick start

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), a Postgres
DSN ([Neon](https://neon.tech) recommended — serverless, scale-to-zero; any
Postgres works), and an [FMP](https://financialmodelingprep.com) API key
(see [Data](#data-you-bring-an-fmp-key) below).

```bash
git clone git@github.com:allenyan513/quant-researcher.git
cd quant-researcher
uv sync

cp .env.example .env          # fill in QR_DATABASE_URL + FMP_API_KEY
$EDITOR .env

uv run qr db ping             # verify the connection
uv run qr db init             # create the schema

cp config/watchlist.sample.txt config/watchlist.txt   # your tickers, one per line
uv run qr universe set --file config/watchlist.txt

uv run qr data refresh --scope all    # first run: ingest everything
uv run qr data freshness              # see what's fresh / stale / missing
```

`qr data refresh` only re-fetches **stale or missing** rows by default (per-scope
thresholds); add `--force` to refresh everything. If your FMP plan excludes
quarterly statements, add `--periods annual`.

## Data: you bring an FMP key

The warehouse is fed by [Financial Modeling Prep](https://financialmodelingprep.com).
You supply your own key — the project ships no data and proxies nothing. It is
designed to **degrade gracefully** when your plan lacks a premium endpoint:

| Data | If your FMP plan lacks it |
|---|---|
| Profiles · daily OHLCV · statements · ratios · estimates | Core warehouse — required for screening / valuation / backtests |
| News (`qr research news`) | **Soft-fails** → bundles still build, just without headlines |
| Earnings transcript (`qr earnings --transcript`) | **Soft-fails** → returns without the excerpt |
| Dividend-adjusted close | **Soft-fails** → falls back to raw close |
| key-metrics (ROE / ROA / FCF-yield) | Per-field hard-fail on that period; `/ratios` rows still ingest |

The core warehouse runs on FMP's entry tiers (rate-limited); premium-only
endpoints fail soft so the rest of the pipeline keeps working. A small sample
dataset for trying the tool **without** a paid key is on the roadmap.

## Holdings: any broker

Holdings come from one of two sources — **you do not need IBKR**:

- **CSV (any broker).** Export positions from Schwab, Fidelity, Robinhood,
  Vanguard, anything — map them to a tiny schema and import. Required columns:
  `account_id, symbol, quantity, as_of_date`; optional `avg_cost, mark_price,
  market_value, currency, asset_category, side, description`.

  ```bash
  uv run qr holdings import-csv --file my_positions.csv
  ```

- **IBKR Flex (optional automation).** If you *do* use Interactive Brokers, set
  `FLEX_TOKEN_KEY` / `FLEX_QUERY_ID_LIVE` and `qr holdings sync` pulls a snapshot
  for you. It's pure convenience layered on top of the same importer.

Each snapshot is keyed by `(account, symbol, as_of_date)`, so daily snapshots
accumulate into a position history.

## The JSON envelope

Every command emits one envelope on stdout (`exit 0` = ok, `1` = error):

```json
{
  "ok": true,
  "schema_version": "1",
  "as_of": "2026-05-23",
  "data_freshness": {"warehouse": "live"},
  "snapshot_id": null,
  "code_version": "git:e61f8b1",
  "data": { "...": "command result" },
  "error": null
}
```

This is the whole integration surface: an agent (or any script) runs `qr ...` via
a shell and consumes structured, timestamped, reproducible results.

## Command reference

| Command | What it does |
|---|---|
| `qr db ping / init / status` | connectivity + latency · create schema · show tables |
| `qr universe set --file PATH` / `list` | replace the watchlist universe |
| `qr data refresh --scope <X> [--force] [--symbols A,B]` | ingest/refresh `X ∈ {profile, quote, financials, ratios, estimates, all}` |
| `qr data freshness` | per-scope fresh / stale / missing report |
| `qr screen run [--expr "..."] [--technical "..."] [--name N]` | fundamental + technical screen |
| `qr screen list / runs / diff / fields` | saved screens · run history · diff two runs · valid fields |
| `qr value SYM [--model dcf\|peg\|multiples\|all] [--assumptions JSON]` | valuation + sensitivity, snapshotted |
| `qr holdings sync / import-csv / list / history` | IBKR Flex or CSV positions |
| `qr morningcall [--account A] [--as-of ...] [--save]` | per-holding + portfolio briefing |
| `qr research bundle SYM` / `news` / `list` / `show` | one-shot research aggregate |
| `qr earnings SYM [--limit N] [--transcript]` | actual-vs-estimate + recorded thesis |
| `qr ledger add SYM --side buy\|sell [...]` / `track` / `scorecard` / `list` / `show` | decision journal + forward alpha |
| `qr signal research --factor F [...]` / `factors` / `list` / `runs` / `show` | factor IC / quantiles / decay |
| `qr backtest run --symbols A --start D --end D (--strategy N\|--strategy-file P)` / `list` / `show` | backtest + persisted metrics |

## Status

**v1 — all eight capability domains are closed.** Built-in backtest strategies:
`sma_crossover`, `buy_and_hold`, `macd_crossover`, `bollinger_reversion`,
`rsi_reversion`, `donchian_breakout`.

Beyond v1 (candidates): EPV / DDM valuation models, reverse DCF, a sample
dataset for keyless trials, multi-broker CSV templates, multi-symbol backtests,
and an optional MCP adapter over the same core library.

## Development

```bash
uv sync
uv run ruff check .        # lint (CI-enforced)
uv run pytest -q           # tests: in-memory SQLite + respx, no real FMP/DB needed
```

CI runs ruff + pytest on every push / PR. Schema changes use SQLAlchemy
declarative models picked up by `qr db init` (additive); see
[CLAUDE.md](CLAUDE.md) for the column-migration workflow.

## Design docs

- [CLAUDE.md](CLAUDE.md) — engineering handbook and contracts for contributors
  (and AI collaborators).
- [docs/features.md](docs/features.md) — requirements and the decision log
  (D1–D12).
- [docs/implementation-plan.md](docs/implementation-plan.md) — implementation
  notes (I1–I8) and milestones.

## License

[MIT](LICENSE) © 2026 allenyan513
