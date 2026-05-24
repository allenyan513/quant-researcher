---
paths:
  - "quant_researcher/signals/**"
---

# signals/ — MG signal research (factor IC / quantiles / decay)

`quant_researcher/signals/` has three layers: `factors.py` (the `REGISTRY` factor
registry) + `panel.py` (warehouse I/O + the point-in-time panel) + `engine.py`
(`run_signal` single entry point + IC/quantile/decay math + persistence). `qr signal
research --factor <name>` ranks the whole universe by a factor on monthly rebalance
dates and measures its power to predict **forward returns**.

- **Factor registry** (`factors.py`): `fundamental` reuses `screen.expression.FIELDS`
  (factor name → financial_ratios column) + `price` (`momentum_12_1/6_1`,
  `reversal_1m`, `realized_vol_3m`, computed from a `PriceSeries`). `direction` (±1/0)
  is used only to align long-short for reporting, **never** to flip raw IC.
- **Point-in-time (PIT) correctness is everything** (`panel.py`): fundamental values
  go through a `FinancialRatios → IncomeStatement` join filtered by
  `IncomeStatement.known_at` (= the real acceptedDate) `<= rebalance_date` — **not**
  `FinancialRatios.known_at` (which is ingestion time and leaks the future). Price
  factors use only bars `<= anchor`. Forward returns use calendar-day `HORIZON_DAYS`;
  momentum uses trading-day row offsets (252/126/21).
- **Efficiency**: `load_price_panel` loads each ticker's price series into a numpy
  `PriceSeries` in one query (`adj_close`, 3-day staleness); forward-return/momentum
  are then in-memory bisects, no per-(symbol,date) queries.
- **Math** (`engine.py`, everything passing through `backtest.runner._to_jsonable` to
  guard numpy/inf/nan): IC = daily `scipy.stats.spearmanr` (**strip None/NaN pairs
  first**, constant input → nan → drop that day); summary emits mean/std/IR/t-stat/
  hit-rate; quantiles via `argsort` + `array_split` into equal buckets → bucket-mean
  returns + long-short spread (raw + direction-aligned) + monotonicity; decay = mean
  IC per horizon.
- **Honest coverage block** (always present): 2 years of prices + ~2 annual reports
  per ticker → fundamental factors are **quasi-static** (few distinct cross-sections,
  autocorrelated IC, inflated t-stats). `coverage.warnings` warns explicitly when a
  fundamental factor is quasi-static / n_dates<6 / avg_symbols<10 — **never inflate IC
  on a thin sample**. The CLI emits `coverage` verbatim.
- **Persistence**: `Signal` (definition) + `SignalRun` (run_id uuid + ic_summary/
  quantiles/decay/coverage JSON), mirroring Screen/ScreenRun.
