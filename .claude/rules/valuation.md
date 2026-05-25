---
paths:
  - "quant_researcher/valuation/**"
---

# valuation/ — MC valuation (layered models + reproducible snapshots)

**Layers** (`quant_researcher/valuation/`)
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
  Python callers both go through it. Each model writes one `valuation_snapshots` row
  (JSON `assumptions` + `result` + `sensitivity`), `code_version` auto-recorded,
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
- EPV / DDM are deferred — same schema, add as new model functions + register in
  `value_company`'s `VALID_MODELS`.

**Phase 2 — reverse DCF + scenario** (`reverse_dcf.py` / `scenario.py`, both pure):
- `reverse_dcf.implied_growth` bisects stage-1 growth so `dcf_fcff` per-share ==
  current price (FV is monotincreasing in growth). It's surfaced as
  `models["dcf"]["reverse"]` (incl. `gap_vs_assumed` / `gap_vs_history`), persisted
  inside the **dcf** snapshot's `result` — it has no fair value of its own, so it is
  NOT a separate `model_type`.
- `scenario` IS a registered `model_type` (bull/base/bear, prob-weighted FV; auto
  base±`scenario_delta`, probs 25/50/25, all `assumptions`-overridable). It writes
  its own `ValuationSnapshot`, so Phase-1's bundle `valuation_snapshots` picks it up.
- **`scenario` is excluded from `fair_value_per_share_mean`** — it's a DCF variant;
  blending it would double-count DCF against the independent peg/multiples reads.
