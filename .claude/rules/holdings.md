---
paths:
  - "quant_researcher/holdings/**"
---

# holdings/ — ME holdings (Flex API two-step + unified importer)

**Flex two-step flow** (`quant_researcher/holdings/ibkr_flex.py`)
1. `SendRequest?t=...&q=...&v=3` → `<FlexStatementResponse>` with a `ReferenceCode`.
2. Poll `GetStatement?t=...&q=<ref>&v=3` — while not ready IBKR returns `ErrorCode
   1019` (Status=Warn, keep polling); once ready it returns `<FlexQueryResponse>`
   and we parse `<OpenPositions><OpenPosition .../></OpenPositions>`.
   `max_poll_attempts=6` × `poll_delay=8s` covers most live queries.

**Schema discovery**: a Flex Query's fields depend on what the user ticked in the
IBKR backend. ME-1 pulled once with the user's real token and confirmed
`position` / `markPrice` / `costBasisPrice` / `fifoPnlUnrealized` / `percentOfNAV` /
`accountId` / `reportDate` are present. Changing the Flex columns is handled
automatically (the importer keeps all attrs in `raw` JSON).

**Unified importer** (`quant_researcher/holdings/importer.py`)
- `import_holdings(session, source="flex"|"csv"|"manual", payload, ...)`, mapping
  internally to unified `Holding` fields.
- Uses `session.merge` — same PK `(account_id, symbol, as_of_date)` overwrites
  (re-running sync the same day updates markPrice without conflict).
- A row missing a PK field goes to `result.skipped` without blocking the rest.

**CSV format**: required `account_id, symbol, quantity, as_of_date` (YYYY-MM-DD);
optional `avg_cost / mark_price / market_value / currency / asset_category / side /
description`. Empty numeric cells become None.

**Trades** (`fetch_trades` + `import_trades`, model `models/trades.py`)
- Same Flex two-step; `_parse(container_tag, row_tag)` is generic — trades parse
  `<Trades><Trade>`. The live query must have the **Trades** section ticked.
- Grain is **per execution (fill)**, PK `(account_id, ib_exec_id)`. `ibExecID` is
  globally unique, so `merge` makes a same-day re-pull idempotent and lets an IBKR
  correction (same execID) overwrite in place.
- **Empty payload is legitimate** (no-trade day) → `import_trades` returns
  `imported=0` instead of raising (unlike `import_holdings`). `qr trades sync` then
  exits 0. Pull "Last Business Day" after the close — intraday Flex is unreliable.
- `executed_at` is stored **verbatim** (Flex `dateTime` format varies, no tz);
  `trade_date` (YYYYMMDD) is parsed via `_parse_flex_date` for day-level queries.

**Gotchas**:
- An OPT position's `symbol` is OCC-style (e.g. `"META  260821P00530000"`, double
  space in the middle) — don't trim/split, store verbatim.
- `position` can be negative (short); `side` is written "Short" accordingly.
- Flex `reportDate` is `YYYYMMDD` (no hyphens); `_parse_flex_date` handles it.
- Never commit the token to the repo; it lives in `.env` only.
