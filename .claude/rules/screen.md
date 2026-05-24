---
paths:
  - "quant_researcher/screen/**"
---

# screen/ — MB screening (AST sandbox + named DSL)

**Fundamental expressions** (`quant_researcher/screen/expression.py`) use
`ast.parse(..., mode='eval')` to parse the string into an AST, then **walk it
manually** — **never calling `eval`**. Allowed-node whitelist: `BoolOp(And|Or)` /
`UnaryOp(Not|USub|UAdd)` / `Compare` / `Name` / `Constant` / `List` / `Tuple`.
Call / Attribute / Subscript / Lambda / comprehensions are all rejected. New fields
must enter the `FIELDS` registry (also the "valid:" list in error messages).

**Technical DSL** (`quant_researcher/screen/technical.py`) is `name[arg1,arg2]`
form, comma-separated, all predicates AND-ed. The predicate registry is `_REGISTRY`
at the bottom; to add one, write a factory returning `Predicate = Callable[[closes,
volumes], bool]`. The parser handles commas nested inside `[…]` (depth tracking).

**State loading** (`quant_researcher/screen/engine.py`) queries each source table
once, then aggregates per symbol in Python (simplified greatest-N-per-group). At 300
tickers × ~10 annual ratios = 3k rows, O(N) Python is plenty. If MG+ needs more
factors, move to SQL window functions and rewrite `build_symbol_state`.

**Adding a field**: add column → add to `FIELDS` registry → write the fill logic in
`build_symbol_state` → add a test → sync docs.

Note: `FIELDS` is also reused as the fundamental-factor source for signal research
(see `signals.md`).
