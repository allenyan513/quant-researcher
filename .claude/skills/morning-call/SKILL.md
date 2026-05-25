---
name: morning-call
description: >-
  Run the user's morning call / pre-open / night-session briefing on their `qr`
  portfolio: the deterministic `qr morningcall` core (holdings, day P&L
  attribution, sector exposure, ledger decisions) plus concurrent web search for
  live context (today's catalysts, overnight analyst rating/price-target changes,
  pre-market + macro tape). Use this skill for "morning call", "morningcall",
  "夜盘报告", "持仓简报", "过一遍持仓", "开盘前看持仓", "pre-open briefing on my
  holdings", "how's my portfolio before the open" — any request to review what the
  user currently holds ahead of the US open, in English or Chinese, bare phrase or
  full sentence, with or without "qr". The defining signal is the user's own held
  positions plus a pre-open / overnight time frame. Do NOT use for generic
  pre-market reports not tied to held positions (those are the IBKR-CSV / Notion
  pre-market-report skills), IBKR trade-history lookups, universe / watchlist
  edits, or standalone DCF / valuation requests.
---

# Morning Call — portfolio briefing

Generate a pre-open briefing for the user's `qr` portfolio. There are **two
layers**: the deterministic core (facts the user owns, from `qr`) and the live
context (today's fast-changing world, from web search). Keep them separate — web
search adds context and narrative, it never overrides a `qr` number.

## Step 1 — Deterministic core (`qr`)

Each `qr` command prints exactly one JSON envelope; parse it, check `ok`, read
`data`.

1. `qr data freshness` — if the `quote` scope lists stale symbols you hold (or
   the benchmarks SPY/QQQ), refresh just those:
   `qr data refresh --scope quote --symbols <stale held + SPY,QQQ>`. Don't
   `--force` the whole universe — it spends FMP quota.
2. `qr morningcall --news 1` — the structured base: per-holding weight / P&L /
   **day P&L** / lean ratios / linked ledger decision, plus the portfolio layer
   (total market value, **`day_pnl` / `day_pnl_pct`**, `top_contributors` /
   `top_detractors` by dollars, sector exposure, `notes`).
3. Record the holdings `as_of_date` and the price date. Surface every entry in
   `notes` (stale prices, excluded cash, mixed currency) **verbatim at the top** —
   do not hide data-quality caveats.

## Step 2 — Live context (concurrent web search)

Launch these as **parallel subagents** — one message, multiple Agent calls — so
they run concurrently and keep raw results out of the main context. Batch the
holdings (~5 symbols per subagent). Instruct each subagent to return findings
**with a source URL each**, and to drop anything it cannot source.

- **Today's catalysts** — per holding: any earnings date in the next ~7 trading
  days; plus today's high-impact US macro prints (CPI / PCE / FOMC / NFP /
  jobless claims).
- **Overnight analyst actions** — rating changes, price-target revisions, and
  initiations on the held names since the last close.
- **Pre-market & macro tape** — pre-market move on the largest positions; index
  futures (ES / NQ), 10Y yield, DXY, WTI, gold, VIX, BTC.

## Step 3 — Synthesize (output in chat)

Write a scannable briefing **directly in the chat**. Do **not** persist it
anywhere unless the user explicitly asks (see Persistence). Suggested structure:

1. **Header** — account, holdings `as_of_date` + price date, total market value,
   **day P&L ($ and %)**, total unrealized P&L. Put any `notes` caveats here.
2. **Movers by dollar** — `top_contributors` / `top_detractors` (then by % only
   if it adds something; a tiny position with a big % move is not a big mover).
3. **Today's catalysts** — your holdings reporting this week + today's macro,
   each with a source link.
4. **Overnight analyst actions** on your names — each with a source link.
5. **Pre-market & macro tape** — futures / rates / VIX / oil + pre-market on the
   big positions.
6. **Ledger cross-check** — open decisions with high `confidence` worth acting on
   at the open (e.g. a high-conviction sell that the tape agrees with).

## Rules

- **`qr` numbers are the system of record.** Web search supplies context only —
  never restate a position size, price, weight, or P&L from search over the `qr`
  value.
- **Cite every external claim** with a source URL. If a search returns nothing
  credible, say so — never fill the gap with a guess.
- The morningcall `valuation` (DCF) runs on default assumptions and is
  unreliable; do not feature it. Mention only if asked, flagged as such.
- **Persistence is opt-in.** Default is chat output only. Only when the user
  explicitly says to save (e.g. "存到 Notion") do you persist — to Notion via the
  Notion MCP, or wherever they specify.
