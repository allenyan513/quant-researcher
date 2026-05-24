# quant-researcher

> An LLM-orchestrated US-equity research substrate. You talk to **Claude Code**
> in plain English — *"deep-dive NVDA", "how's my portfolio doing", "find cheap
> quality compounders"* — and it drives the data warehouse, screens, valuation,
> backtests, and decision ledger underneath, then writes the answer. **You never
> touch a command line.**

[![CI](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml/badge.svg)](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)

## How you use it

You don't run `qr` commands by hand. You set it up once (below), then talk to
**Claude Code** — the terminal AI agent — in natural language. Claude reads
[`CLAUDE.md`](CLAUDE.md), decomposes your request into the right `qr` calls,
chains them, and synthesizes the answer.

| You ask Claude… | …and it orchestrates under the hood |
|---|---|
| *"Screen for cheap quality compounders, then value the top 3."* | fundamental screen → DCF / PEG / multiples on the best names |
| *"Deep-dive NVDA before I add to my position."* | research bundle → earnings actual-vs-estimate → valuation |
| *"What moved in my portfolio overnight, and why?"* | morning call — per-holding view + portfolio aggregates |
| *"Did my January buys actually beat the market?"* | forward-alpha tracking vs SPY / sector → scorecard |
| *"Is a 20/50 SMA crossover any good on AAPL the last two years?"* | backtest → metrics + equity curve |
| *"Has momentum predicted returns across my universe?"* | factor IC / quantile spread / decay |

The system never interprets your intent or writes prose — that's Claude's job. It
only does **fetch → compute → persist → reproduce**, behind a stable
JSON-on-stdout contract an agent can chain reliably.

## Why it's built this way

The properties that make it safe to hand an agent your research:

- **Point-in-time correct.** Financial statements are stamped with FMP's
  `acceptedDate`, so screens and backtests see only what was knowable on the
  as-of date. No lookahead bias.
- **Reproducible and auditable.** Every result carries `as_of`, `code_version`,
  and an optional snapshot id. A decision snapshots the exact data it was made
  on — so you can grade the agent's judgment months later.
- **Agent-first contract, not a black box.** One JSON envelope per command; the
  agent composes small primitives instead of calling one end-to-end oracle, so
  you can see and trust every step.
- **Yours, low-config.** Built around a personal IBKR + CSV + Claude-skills
  workflow. Opinionated defaults over endless configuration.

## What your agent can do — eight capability domains

| | Domain | What Claude can do for you |
|---|---|---|
| **A** | Data warehouse | keep a point-in-time FMP-fed warehouse fresh, refreshing only what's stale |
| **B** | Screening | fundamental expressions **and** technical scans, combined, saved, and diffed over time |
| **C** | Valuation | DCF-FCFF + PEG + relative multiples with a sensitivity grid, every run snapshotted |
| **D** | Research & earnings | one-shot research bundle; earnings actual-vs-estimate with surprise |
| **E** | Holdings & morning call | positions from IBKR **or any-broker CSV**; a lean portfolio briefing |
| **F** | Decision ledger | record a thesis + data snapshot, then track 1w–6m alpha vs SPY / sector |
| **G** | Signal research | factor IC / quantile spread / decay, strictly point-in-time |
| **H** | Backtesting | an event-driven engine, 6 built-in strategies + your own strategy files |

## One-time setup

You (the human) do this once. After that, it's all natural language.

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), a Postgres DSN
([Neon](https://neon.tech) recommended — serverless, scale-to-zero; any Postgres
works), and an [FMP](https://financialmodelingprep.com) API key (see
[Data](#data-you-bring-an-fmp-key)).

```bash
git clone git@github.com:allenyan513/quant-researcher.git
cd quant-researcher
uv sync

cp .env.example .env            # fill in QR_DATABASE_URL + FMP_API_KEY
$EDITOR .env

uv run qr db ping               # verify the connection
uv run qr db init               # create the schema

cp config/watchlist.sample.txt config/watchlist.txt   # your tickers, one per line
uv run qr universe set --file config/watchlist.txt
uv run qr data refresh --scope all                    # first ingest
```

Then open Claude Code in this repo and just talk to it. Claude refreshes stale
data itself when needed — you don't manage that.

## Data: you bring an FMP key

The warehouse is fed by [Financial Modeling Prep](https://financialmodelingprep.com).
You supply your own key — the project ships no data and proxies nothing. It is
designed to **degrade gracefully** when your plan lacks a premium endpoint, so
Claude can keep working:

| Data | If your FMP plan lacks it |
|---|---|
| Profiles · daily OHLCV · statements · ratios · estimates | Core warehouse — required for screening / valuation / backtests |
| News | **Soft-fails** → research bundles still build, just without headlines |
| Earnings transcript | **Soft-fails** → returns without the excerpt |
| Dividend-adjusted close | **Soft-fails** → falls back to raw close |
| key-metrics (ROE / ROA / FCF-yield) | Per-field hard-fail on that period; `/ratios` rows still ingest |

The core warehouse runs on FMP's entry tiers (rate-limited); premium-only
endpoints fail soft so the rest keeps working. A small sample dataset for trying
the tool **without** a paid key is on the roadmap.

## Holdings: any broker

You **do not need IBKR**. Holdings come from either:

- **CSV (any broker).** Export positions from Schwab, Fidelity, Robinhood,
  Vanguard — anything — and Claude imports them (required columns: `account_id,
  symbol, quantity, as_of_date`; common optionals like `avg_cost`, `mark_price`
  supported).
- **IBKR Flex (optional automation).** If you *do* use Interactive Brokers, set
  `FLEX_TOKEN_KEY` / `FLEX_QUERY_ID_LIVE` and Claude can pull a snapshot for you.
  It's pure convenience over the same importer.

## For the agent (and contributors)

[`CLAUDE.md`](CLAUDE.md) is the agent's manual: the full command surface, the
JSON envelope contract, and the **"natural language → `qr` orchestration"** guide
Claude follows. It doubles as the engineering handbook if you're modifying
quant-researcher itself (`uv run ruff check . && uv run pytest -q` before any PR).
As a human *using* the tool, you rarely need to open it.

## Status

**v1 — all eight capability domains are closed.**

## License

[MIT](LICENSE) © 2026 allenyan513
