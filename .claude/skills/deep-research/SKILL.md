---
name: deep-research
description: >-
  Run a deep, institutional-style research dive on ONE stock using the user's `qr`
  warehouse: the deterministic core (`qr research bundle` quality/quant scores +
  `qr earnings` actual-vs-estimate + `qr value --model all` with reverse DCF and
  bull/base/bear scenario + the latest earnings-call transcript) plus concurrent
  web search for live context (analyst actions, catalysts, competitive/expert
  color, the prevailing narrative), synthesized into a cited bull/base/bear thesis.
  Use this skill for "research NVDA", "deep-dive AAPL", "deep dive on TSLA", "build
  a thesis on MSFT", "is GOOG a buy", "研究一下 英伟达", "深度调研 NVDA",
  "做个 X 的深度分析", "X 值不值得买" — any request to analyze / value / form a view
  on a SINGLE named ticker, in English or Chinese, with or without "qr". The
  defining signal is one symbol plus an analyze / value / thesis intent. Do NOT use
  for: reviewing the user's held portfolio before the open (that's the morning-call
  skill), generic pre-market reports, screening/watchlist edits, IBKR trade-history
  lookups, or a bare valuation number with no thesis (use `qr value` directly).
---

# Deep Research — single-stock deep dive

Build a deep-dive thesis on ONE ticker. There are **two layers**: the
deterministic core (facts from the `qr` warehouse) and the live context (today's
fast-changing world, from web search). Keep them separate — web search adds
context and narrative; it never overrides a `qr` number.

> **Invocation:** `qr` is a project script (`pyproject.toml [project.scripts]`).
> A bare `qr` is usually not on PATH — run it as **`uv run qr …`** from the repo
> root. The examples below write `qr` for brevity.

## Step 1 — Deterministic core (`qr`)

Each `qr` command prints exactly one JSON envelope; parse it, check `ok`, read
`data`. Run these for the one symbol (`SYM`):

1. `qr data freshness --symbols SYM` — if anything's stale, refresh the
   fundamentals in **one call**: `qr data refresh --scope all --symbols SYM`
   (only-stale by default; covers profile / quote / financials / ratios /
   estimates). Then pull the two scopes excluded from `all` (their own free
   sources): `qr data refresh --scope transcript --symbols SYM` (Alpha Vantage —
   soft-skips if unavailable) and `qr data refresh --scope insider --symbols SYM`
   (SEC Form 4 via EDGAR). Don't `--force`; it spends FMP quota.
2. `qr research bundle SYM` — the structured base. Read:
   - `profile` · `latest_price` · `ratios_latest_annual` (incl. `roic`,
     `earnings_yield`)
   - **`scores`** — Piotroski F (x/9, with `missing` legs) + Altman **Z''** (zone)
   - **`quality`** — `roic_wacc_spread`, `fcf_conversion`, `accruals_ratio`, and
     multi-year `trends` (revenue / margins / ROIC)
   - **`ratio_history`** — multi-year multiples + `latest_percentile_vs_history`
     (cheap/expensive vs the stock's own past)
   - `valuation_snapshots` · **`transcript`** (latest call: year / quarter /
     call_date / ~2000-char excerpt — for fuller speaker-segmented text +
     sentiment, read the `transcripts` table directly) · **`insider`** (recent
     Form 4 open-market buy/sell tally + notable trades) · `news` · `holdings`
     (your position + cost basis, if any)
   - Any section is `null` when its data is missing — say so, don't invent it.
3. `qr earnings SYM` — actual-vs-estimate EPS/revenue surprise (only where an
   estimate exists — historical surprise is **sparse**; never imply a beat/miss
   without one) + any recorded ledger thesis/decisions.
4. `qr value SYM --model all` — DCF · PEG · multiples · **scenario** (bull/base/
   bear, probability-weighted FV) plus the DCF block's **`reverse`** (the growth
   the current price implies + `gap_vs_assumed` / `gap_vs_history`). Cite the
   `snapshot_id`s so the call can be replayed/graded later.

## Step 2 — Live context (concurrent web search)

Launch these as **parallel subagents** — one message, multiple Agent calls — so
they run concurrently and keep raw results out of the main context. Instruct each
to return findings **with a source URL each**, and to drop anything it cannot
source.

- **Analyst actions** — recent rating changes, price-target revisions, and new
  initiations on `SYM`.
- **Catalysts & events** — next earnings date; product launches, regulatory /
  legal items, investor days, and any near-term binary events.
- **Competitive / industry / expert color** — moat & market-share dynamics,
  channel-check / expert-call themes, industry tail/headwinds; the bull case vs
  the bear case.
- **Recent narrative** — material headlines over the last ~2 weeks, the prevailing
  narrative, and the credible contrarian take.

## Step 3 — Synthesize (output in chat)

Write a scannable, institutional-style deep dive **directly in the chat**. Do
**not** persist it unless asked (see Persistence). Every `qr` number is owned by
`qr`; every external claim carries a source link. Suggested structure:

1. **Snapshot** — name / sector / price / market cap, `as_of` + any data caveats
   from `notes`; your current position if held.
2. **Business & segments** — what drives revenue; TAM / growth drivers (web, cited).
3. **Moat** — durability via Hamilton Helmer's 7 Powers; competitive threats (cited).
4. **Financial quality** — ROIC vs WACC spread, FCF conversion, accruals, and
   margin/ROIC/revenue **trends** (Phase 1 `quality`); Piotroski **x/9** + Altman
   **Z'' zone** — name any `missing` Piotroski legs honestly.
5. **Valuation** — lead with the **reverse-DCF expectations gap** ("price implies
   ~X% growth vs ~Y% assumed / historical") and the **scenario bull/base/bear**
   band; multiples **vs the stock's own history** (percentile) and vs peers. The
   forward-DCF point estimate runs on default assumptions and is unreliable — do
   **not** feature it; present valuation via the reverse read + scenario band.
6. **Management & capital allocation** — buybacks / dividends / M&A; **transcript
   guidance + Q&A highlights** (Phase 3 — cite the specific call).
7. **Catalysts** — near-term events + the next earnings date (cited).
8. **Risks** — financial-quality flags (low F-score, weak accruals, distress-zone
   Z'') + web-sourced risks.
9. **Ownership / positioning** — your holding (`holdings`) + **insider activity**
   from `qr`'s `insider` section (open-market buys vs sells, notable Form 4s).
   13F institutional ownership / short interest aren't ingested yet → source from
   the web if relevant (cited).
10. **Thesis & recommendation** — bull / base / bear and your conviction. Offer to
    record it: `qr ledger add SYM --side buy|sell --thesis "…" --confidence N`,
    citing the valuation `snapshot_id` so forward alpha can be graded later.

## Rules

- **`qr` numbers are the system of record.** Web search supplies context only —
  never restate a price, ratio, score, or fair value from search over the `qr`
  value.
- **Cite every external claim** with a source URL. If a search returns nothing
  credible, say so — never fill the gap with a guess.
- **Reverse DCF is the value-investor headline** (what growth the price bakes in).
  The plain forward DCF on default assumptions is unreliable — present valuation
  through the reverse read + the scenario band, not a single point estimate.
- **Be honest about coverage.** Surface `notes` from `qr earnings` / `qr value`
  verbatim; flag any `null` bundle section as missing data; name `missing`
  Piotroski legs; flag sparse earnings surprise; note the transcript `call_date`
  is a derived quarter-end placeholder (Alpha Vantage omits the exact date), and
  the transcript / insider scopes soft-skip when unavailable.
- **Persistence is opt-in.** Default is chat output only. Record a decision to the
  ledger (or save elsewhere, e.g. Notion) only when the user explicitly asks.
