---
paths:
  - "tests/**"
---

# tests/ — testing conventions

- **DB**: in-memory SQLite. Fixture pattern:
  ```python
  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite://", future=True)
      Base.metadata.create_all(engine)
      with Session(engine, future=True) as sess:
          yield sess
  ```
  CLI tests use the `memory_db` fixture to patch `session_factory` to SQLite.
- **FMP business tests** (`test_refresh.py`): `MagicMock(spec=FMPClient)`, set
  `return_value` / `side_effect` per method.
- **FMP HTTP tests** (`test_fmp.py`): `respx.mock` + `httpx.Response(...)`.
- **TZ gotcha**: SQLite doesn't store tz. To compare a `DateTime(timezone=True)`
  column, normalize with the `_naive_utc(dt)` helper (see `tests/test_refresh.py`).
- **CLI tests**: `from typer.testing import CliRunner` + `_json_lines(output)` to
  parse multi-line envelopes, **asserting exactly 1**.
- **ruff `B008`** is already ignored (typer Option defaults are a documented
  pattern); don't change it back.
- `tests/engine/` is the upstream quant-engine test port (235) — change imports only.
