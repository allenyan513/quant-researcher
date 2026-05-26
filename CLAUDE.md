# CLAUDE.md

> Manual for Claude Code working with quant-researcher. You're in one of two roles:
>
> - **Operating the tool** — a human asked you to do research ("deep-dive NVDA",
>   "how's my portfolio"). Read **§0 — Operating this tool** below.
> - **Modifying this codebase** — §1–§3 are the global CLI contracts that *any*
>   change can trip. **Per-domain contracts auto-load from `.claude/rules/`** when you
>   edit that domain (editing `quant_researcher/valuation/**` pulls in `valuation.md`,
>   etc.) — you don't need to read them all up front.
>
> User-facing pitch: [`README.md`](README.md). Code status: **v1 — all eight
> capability domains (M0 + MA–MH) closed.**

## §0 — Operating this tool (you are the agent a human drives)

A human talks to you in plain English — *"research NVDA", "how's my portfolio",
"find cheap compounders"*. Your job: decompose that into `qr` calls, chain them,
and **synthesize the answer yourself**. The human never sees the commands — they
read your prose.

**The contract.** Every `qr` subcommand prints **exactly one JSON envelope** on
stdout — `{ok, data, as_of, data_freshness, code_version, error}` — exit 0 = ok,
1 = error. Parse it, check `ok`, read `data`, chain the next call.

**Command surface** (`qr <group> <cmd>`; run `qr ... --help` for full flags):

| Command | What it does |
|---|---|
| `qr db ping / init / status` | connectivity + latency · create schema · show tables |
| `qr universe set --file PATH` / `list` | replace the watchlist universe |
| `qr data refresh --scope <X> [--force] [--symbols A,B]` | ingest/refresh `X ∈ {profile, quote, financials, ratios, estimates, all}` |
| `qr data freshness` | per-scope fresh / stale / missing report |
| `qr screen run [--expr "..."] [--technical "..."] [--name N]` | fundamental + technical screen |
| `qr screen list / runs / diff / fields` | saved screens · run history · diff two runs · valid fields |
| `qr value SYM [--model dcf\|peg\|multiples\|all] [--assumptions JSON]` | valuation + sensitivity, snapshotted |
| `qr holdings sync / import-csv / list / history` | IBKR Flex or CSV positions |
| `qr trades sync / list` | IBKR Flex executed fills (per-execution, idempotent) |
| `qr morningcall [--account A] [--as-of ...] [--save]` | per-holding + portfolio briefing |
| `qr research bundle SYM` / `news` / `list` / `show` | one-shot research aggregate |
| `qr earnings SYM [--limit N] [--transcript]` | actual-vs-estimate + recorded thesis |
| `qr ledger add SYM --side buy\|sell [...]` / `track` / `scorecard` / `list` / `show` | decision journal + forward alpha |
| `qr signal research --factor F [...]` / `factors` / `list` / `runs` / `show` | factor IC / quantiles / decay |
| `qr backtest run --symbols A --start D --end D (--strategy N\|--strategy-file P)` / `list` / `show` | backtest + persisted metrics |

**Natural language → orchestration** (worked patterns; adapt, don't follow blindly):

| The human asks… | Your chain |
|---|---|
| "research / deep-dive SYM" | `qr data freshness` → refresh stale scopes → `qr research bundle SYM` → `qr earnings SYM` → `qr value SYM` → synthesize a thesis |
| "screen for X, value the best" | `qr screen run --expr/--technical "..."` → `qr value` on the top names |
| "morning call / how's my portfolio" | `qr morningcall` (optionally `--save`) |
| "did my decisions beat the market" | `qr ledger track` → `qr ledger scorecard --group-by confidence\|sector\|tag` |
| "backtest STRATEGY on SYM" | `qr backtest run ...` → `qr backtest show <run_id>` for the curve/trades |
| "is factor F predictive" | `qr signal research --factor F --horizon ...` |
| "value SYM / is it cheap" | `qr value SYM --model all` (+ `qr screen` for a relative read) |
| "record that I bought SYM because…" | `qr ledger add SYM --side buy --thesis "..." --confidence N` |
| "what did I actually trade (last session)" | `qr trades sync` → `qr trades list [--symbol SYM] [--since DATE]` |

**Orchestration rules:**

- **Freshness first.** Before reading warehouse data for a symbol, consider
  `qr data freshness`; refresh stale scopes (the report's `stale_symbols` feeds
  `qr data refresh --symbols`). Don't blindly `--force` — it spends FMP quota.
- **Benchmarks must exist.** Ledger alpha needs SPY + sector ETFs in the universe
  and quote-refreshed, or the alpha column is null. Add them via `qr universe set`.
- **Soft-fails are fine.** News / transcript / dividend-adjusted 402 → the rest
  still works; never abort the chain over a missing premium endpoint.
- **You write the prose.** The tool returns structured data only — the narrative,
  judgment, and recommendation are yours. Don't dump raw envelopes at the human.
- **Reproducibility.** `qr ledger add` snapshots the data behind a decision; cite
  `snapshot_id` / `as_of` so the call can be replayed and graded later.
- **Reports go to `~/qr-reports/`, not `/tmp`.** Any generated HTML / PDF deep-dive
  the maintainer might want to keep — deep-research reports, comparison docs,
  sell-put briefs, post-mortems — is written to
  `~/qr-reports/{TICKER}/{YYYY-MM-DD}-{TICKER}-{type}.html` so it survives
  shell restarts. Types in use: `deep-dive` (standard skill output),
  `comparison` (before/after a fix), `sell-put` / `sell-call` (options-overlay
  variant), `morning-call` (pre-open snapshot). Create the per-ticker
  subdirectory if missing. Append a row to `~/qr-reports/INDEX.md` with the
  date / ticker / type / one-line thesis / link so the maintainer can scan
  the catalog without crawling the tree. Throwaway scratch (audit scripts,
  /tmp jq one-liners) still goes to `/tmp`; only the keeper artifacts go to
  `~/qr-reports/`.

## Commands

```bash
uv sync                          # deps
uv run ruff check .              # lint (must be clean); --fix autofixes imports
uv run pytest -q                 # tests (in-memory SQLite, no real DB / FMP)
uv run pytest -k pattern         # filter by name
```

**Before any PR**: `uv run ruff check . && uv run pytest -q` both green (CI runs these too).

## Global CLI contracts (any command can trip these)

### 1. JSON envelope: exactly one per command

Every `qr` subcommand emits **one** envelope to stdout via `quant_researcher/contract.py`'s
`Envelope`, exit code 0=ok / 1=error. Lock-in tests: `tests/test_cli.py::test_*_single_envelope*`.

### 2. `_emit` inside a `try` double-emits the envelope ⚠

`_emit(envelope)` internally does `raise typer.Exit(code)` — and `typer.Exit` is a
subclass of `Exception`. So a validation `_emit` inside a `try` gets caught by the
outer `except`, which emits a **second** failure envelope. Keep validation emits
**outside** the try; the try wraps only code that throws real business exceptions:

```python
# ❌ Wrong — typer.Exit caught by except → double envelope
try:
    if bad: _emit(Envelope.failure(...))   # raises typer.Exit
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))           # fires twice

# ✅ Right
if bad: _emit(Envelope.failure(...))
try:
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))
else:
    _emit(Envelope.success(...))
```

MA-1 hit this (fixed in MA-2 `39aeb44`). New CLI commands must follow this shape.

### 3. Lazy-import heavy modules inside subcommands

`--help` must not trigger DB / FMP module loading. Import inside the command body:

```python
@data_app.command("refresh")
def data_refresh(...) -> None:
    from quant_researcher.data.fmp import FMPClient        # lazy
    from quant_researcher.data.refresh import refresh_X    # lazy
    from quant_researcher.db import session_factory        # lazy
    ...
```

## Common gotchas (cross-cutting)

- **typer.Exit is an Exception** → see §2; the single most common CLI bug here.
- **`session.scalars(select(a, b, c))` returns only the first column**; for a
  multi-column tuple use `session.execute(select(a, b, c))` then `for row in result`.
- **SQLite has no tz**: in tests, normalize `DateTime(timezone=True)` columns with
  the `_naive_utc` helper; production Postgres has no such problem.
- **ruff** `select = ["E", "F", "W", "I", "B", "UP"]` + `ignore = ["B008"]` (typer
  Option defaults). Don't change lint rules without discussing first.

## Where the rest lives

- **Per-domain contracts** → `.claude/rules/*.md`, path-scoped so each loads only
  when you edit the matching code: `data` · `models` · `screen` · `valuation` ·
  `holdings` · `research` · `ledger` · `backtest`(+`engine`) · `signals` · `testing`.
  Editing a file under `quant_researcher/<domain>/` auto-loads its rule.
- **Key entry points** (read the tree for the rest): `cli.py` (all subcommands;
  lazy-import; `_emit` outside try), `contract.py` (Envelope + `SCHEMA_VERSION`),
  `db.py` (Base + bottom-of-file model side-effect import), `config.py` (DSN normalize).

## Collaboration conventions (meta-spec for collaborating Claudes)

- **requirements-first**: the maintainer prefers discussing requirements/approach
  before writing code. For complex changes, get sign-off first — don't jump straight
  to Write/Edit.
- **Communication**: match the language the maintainer writes in (they are most
  comfortable in Chinese). Code, comments, commits, and PR titles are always in
  **English** (for international readability).
- **One PR per milestone**. Track subtasks with TaskCreate; don't split into many PRs.
- **E2E is Claude's job**: after implementing a change, run end-to-end verification
  **yourself** — real CLI commands against the live DB / live integrations, not
  just `pytest`. Examples that count as e2e: spot-check the affected `qr` command
  on a couple of real symbols; compare typed columns against the source `raw`
  blob; run a downstream consumer (`qr research bundle`, `qr value`, `qr morningcall`)
  to confirm nothing crashes. Present a structured e2e report; the maintainer
  decides whether to commit / open a PR based on it. **Do not wait** for them to
  run the e2e themselves.
- **Ask before a commit/PR**: with the e2e report in hand, still wait for the
  maintainer's explicit go-ahead before `git commit` / `gh pr create`.
- **Minimal diff (surgical)**: change only the lines the task requires. Don't
  "optimize" nearby code in passing, don't refactor what isn't broken, **match the
  existing style** (especially ported `engine/` code). Clean up only orphans **you**
  created; found pre-existing dead code → mention it, don't delete unilaterally.
  Unrelated changes go in **a separate PR**.
- **Simplicity first**: the least code that solves the problem; no beyond-requirement
  features, no abstraction for needs that haven't arrived, no error handling for
  impossible cases. Ask yourself "would a senior engineer think this is over-engineered?".
- **File incidental findings as GitHub issues**: while working on the assigned task,
  if you stumble on a real bug or a worthwhile optimization that's **out of scope**
  for the current change, capture it as a GitHub issue on `allenyan513/quant-researcher`
  instead of silently fixing it (violates "Minimal diff") or losing it in chat scrollback.
  - **What qualifies**: actual bugs (wrong output, crash, contract violation), perf
    issues with concrete impact, missing test coverage on a real risk surface,
    duplicated logic that's already biting. **Does NOT qualify**: style nits,
    speculative refactors, "nice-to-have" features without a triggering pain point,
    anything already tracked.
  - **Ask first, then file**: surface the finding in chat with a one-line title +
    why-it-matters and ask the maintainer to confirm before calling `mcp__github__issue_write`.
    Batch multiple findings into one confirmation prompt rather than asking N times.
  - **Required context in the issue body**: (1) where — `file:line` or `qr <cmd>`
    repro; (2) what's wrong / what's the win; (3) how you found it (the task /
    PR / commit that surfaced it); (4) suggested fix sketch if obvious, otherwise
    "needs investigation". Label with `claude-found` (create the label if missing)
    so the maintainer can filter.
