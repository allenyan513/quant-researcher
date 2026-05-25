---
paths:
  - "quant_researcher/models/**"
---

# models/ — registration, known_at, schema evolution

## Model registration = side-effect import

`Base.metadata` only sees model classes that have been imported. At the bottom of
`quant_researcher/db.py`:

```python
# `Base` is already defined. The line below pulls the model classes in.
from quant_researcher import models  # noqa: E402, F401
```

New model → `quant_researcher/models/X.py` → `from ... import X` in
`quant_researcher/models/__init__.py` and add it to `__all__`. **Don't forget
`__init__.py`**, or `qr db init` won't see your table.

## `known_at` semantics are split (D6)

| Table | `known_at` source | `server_default` |
|---|---|---|
| `securities`, `universe`, `profiles`, `daily_prices` | `func.now()` (ingestion ≈ public time, good enough) | ✅ |
| `income_statement`, `balance_sheet`, `cash_flow` | **parsed from FMP `acceptedDate`** — strict per D6 | ❌ (set in code) |
| `financial_ratios`, `analyst_estimates` | `datetime.now(UTC)` (endpoint gives no acceptedDate) | ❌ (code uses `now(UTC)`) |

Test lock: `tests/test_models.py::test_ma3_known_at_has_no_server_default`.
**Don't add `server_default=func.now()` to the three statement tables** — it breaks
point-in-time queries.

## Schema evolution (D11: no Alembic)

- **New table**: `models/X.py` + re-export in `models/__init__.py` → `qr db init`
  applies it automatically.
- **New (nullable) column**: edit the model → `qr db init` will **not** auto-ALTER
  existing tables. ALTER manually in the Neon console's SQL Editor (`ALTER TABLE X
  ADD COLUMN ...`). Example: `financial_ratios`'s `return_on_invested_capital` /
  `earnings_yield` were added this way (`ALTER TABLE financial_ratios ADD COLUMN
  return_on_invested_capital double precision; ADD COLUMN earnings_yield double
  precision;`), then backfilled with `qr data refresh --scope ratios --force`.
  Phase-1 deep-dive added `balance_sheet.retained_earnings` / `current_assets` /
  `current_liabilities` the same way (`ALTER TABLE balance_sheet ADD COLUMN
  retained_earnings double precision; ADD COLUMN current_assets double precision;
  ADD COLUMN current_liabilities double precision;`). **Backfilling a new
  *statement* column differs from ratios**: `_ingest_statement` is insert-only
  (filed statements are immutable), so `qr data refresh --force` re-fetches but
  *skips* existing rows and will NOT populate the new column. Backfill existing
  rows from the stored `raw` JSON instead (zero FMP): `UPDATE balance_sheet SET
  current_assets = NULLIF(raw->>'totalCurrentAssets','')::double precision, …`.
  Rows from future quarters get the column automatically via the refresh mapper.
- **Change / drop a column**: manual SQL in the Neon console, in order: edit model
  + run tests → ALTER prod → deploy. ⚠ For an **add**, the order is the same:
  ALTER prod **before** deploying the model change, or ORM reads that `SELECT` the
  new column hit "column does not exist" until the ALTER lands.

## gotcha

- **`Base.metadata.create_all(checkfirst=True)` doesn't modify existing tables**;
  don't expect it to auto-sync with the models. Additive only.
