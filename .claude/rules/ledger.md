---
paths:
  - "quant_researcher/ledger/**"
---

# ledger/ — MF decision ledger (record / track / scorecard)

**3 entry points** (`quant_researcher/ledger/engine.py`)
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
- `scorecard(session, group_by, horizon)` — pull Decision + tracking rows, aggregate
  in Python by group_by ∈ {confidence, sector, tag}, return sorted by avg_alpha desc.
  tag is a list → one decision enters N tag groups.

**Key design points**
- **Alpha**: `alpha = return − benchmark`; benchmark prefers the sector ETF
  (`sectors.etf_for_sector`), falling back to SPY when no match.
- **Short decisions**: with `side="sell"`, `return_pct = -(end/start - 1)`, so a 10%
  price drop → +10%.
- **Price staleness window = 3 days**: `_price_near_date` returns None when there's
  no bar within ±3 days of target_date, avoiding using a month-start price as a
  month-end one (weekends + 1 holiday is enough; a longer gap is a data problem).
- **Sector ETF mapping** is a hardcoded constant (`quant_researcher/ledger/sectors.py`),
  lowercase match, missing → fall back to SPY. FMP's sector strings vary
  (`"Financial Services"` vs `"Financials"`); the map holds both.

**Note: SPY and sector ETFs are not universe members by default** — you must
manually `qr universe set` to add SPY / XLK / XLE etc. and `qr data refresh --scope
quote` so the scorecard has benchmark data. Otherwise the alpha column is None.
