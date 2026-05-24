---
paths:
  - "quant_researcher/backtest/**"
  - "quant_researcher/engine/**"
---

# backtest/ + engine/ — MH backtest (quant-engine port + warehouse feed)

**`quant_researcher/engine/` is a wholesale port of quant-engine** (verbatim, only
the `engine.*` → `quant_researcher.engine.*` import prefix changed). **Before
changing it, decide whether you want to stay re-syncable with upstream** — large
edits make future upstream syncs hard. The only minimal changes made: ①
`data/data_feed.py` drops `YFinanceFeed` (removes the yfinance dep, keeps the
`DataFeed` ABC + `CSVFeed`); ② `analytics/metrics.py` drops the yfinance/matplotlib
version reporting; ③ `engine.py`'s `_fetch_spy_benchmark` now reads `benchmark_symbol`
from the injected `data_feed` (the original used yfinance to auto-pull SPY), plus a
`verbose` flag (default True; the CLI path passes `verbose=False` to silence prints,
preserving the single-envelope contract). **Dropped**: `export/ optimize/
data/cached_feed.py analytics/{chart,enhanced_charts,report}.py` (charting/QC/
walk-forward not in v1). The risk/margin/stop modules are **ported but not wired into
the CLI in v1** (`risk_manager=None`).

**qr-specific orchestration in `quant_researcher/backtest/`** (doesn't pollute the
engine package, re-sync friendly):
- `engine/data/warehouse_feed.py` — `WarehouseDataFeed(DataFeed).fetch()` reads
  `daily_prices` → `Bar`. **Default `adjusted=True`**: uses `factor = adj_close/close`
  to adjust the whole OHLC bar (split/dividend correct), `close = adj_close`; missing
  `adj_close` → factor=1; rows with no close are skipped. `--raw` turns it off. This
  is the only qr-specific file under engine/data/ (additive, no upstream conflict).
- `backtest/strategies/` — built-in strategy registry (`REGISTRY` dict; v1 has 6
  single-symbol strategies: `sma_crossover` / `buy_and_hold` / `macd_crossover` /
  `bollinger_reversion` / `rsi_reversion` / `donchian_breakout`). To add a built-in:
  drop a module + register it in `REGISTRY` (the keys also drive the CLI's "valid:"
  error list).
- `backtest/loader.py` — `--strategy-file` uses importlib to load a `BaseStrategy`
  subclass from an external `.py` (**runs locally, not sandboxed** — same trust level
  as running any local script).
- `backtest/runner.py` — `run_backtest(...)` is the single entry point (CLI + Python
  both use it). Resolve strategy (file beats registry name) → single-symbol
  strategies auto-inject `symbols[0]` → run `BacktestEngine(verbose=False)` →
  `calculate_metrics` → write one `backtest_runs` row → return an envelope-friendly
  summary (**without** the large equity_curve/trade_log fields, which go to DB and
  are fetched by `qr backtest show`).
- **Two JSON-serialization gotchas** (both handled in runner): `calculate_metrics`
  emits **numpy scalars** → `_to_jsonable` converts to native; `profit_factor` etc.
  can be **inf/nan** → converted to None (Postgres JSONB rejects Infinity). New fields
  written to `backtest_runs` JSON columns must pass through `_to_jsonable`/`_num`.

**Deps**: the port pulls in `scipy` (metrics' PSR/skew/kurtosis). **Tests**:
`tests/engine/` is a wholesale port of the upstream tests (235, verifying port
correctness, change imports only); qr-specific in `tests/test_warehouse_feed.py` /
`test_backtest_runner.py` / `test_backtest_cli.py`.

**Known limitations (upstream, untouched in v1)** — flagged in the PR #6 review,
**deliberately deferred** because fixing them diverges from upstream and v1 doesn't
need them (to fix for real, change quant-engine upstream first, then sync):
- **Multi-symbol bar misalignment → lookahead bias** (`engine.py` event loop): the
  loop `advance()`s each symbol independently over `range(max_bars)`, **assuming all
  symbols' bars are perfectly aligned**. If a symbol has a gap (halt/late listing) its
  bars shift left relative to others, and one `on_bar` sees prices from different
  dates. v1's built-in strategies are all single-symbol, and the warehouse is a shared
  US trading calendar (same-period symbols are naturally aligned), so it doesn't
  trigger; **multi-symbol strategies (via `--strategy-file`) must ensure their symbols'
  histories are complete**. The fix is to iterate a deduped, sorted union timeline.
- **STOP_LIMIT trigger state isn't persisted** (`broker.py` `_fill_stop_limit`):
  after a stop triggers, if the limit doesn't fill on that bar it just returns None
  **without converting to a LIMIT order**, so the next bar re-evaluates the trigger —
  price crossing back un-triggers it. v1 uses market orders only (`buy`/`sell`), so
  it's untouched; custom strategies using `set_stop_loss` etc. should beware. The fix
  is to mark the order triggered and convert to LIMIT.
