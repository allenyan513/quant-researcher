# CLAUDE.md

> 给 Claude Code(或任何 Claude 风格的 AI 协作者)看的工程手册。**先读这个再动代码。** 用户视角的 quick start 在 [`README.md`](README.md);设计决策在 [`docs/`](docs/)。

## 项目脉络(必读)

权威文档,改代码前先看:

- [`docs/features.md`](docs/features.md) — 需求 v1.0,**决策记录 D1–D11**。需求改了 → 这里加新 D。
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — 实现 v1.0,**I1–I8 + 里程碑 M0–MH**。实现策略变了 → 这里改。

代码现状:**M0 + MA-1/2/3 已合并 master**,下一里程碑是 **MA-4**(`qr data freshness` + 默认只刷陈旧行)。

## 命令(运行前必看)

```bash
uv sync                          # 依赖
uv run ruff check .              # lint(必须干净)
uv run ruff check --fix .        # autofix import 排序等
uv run pytest -q                 # 测试(in-memory SQLite,不依赖真实 DB / FMP)
uv run pytest tests/test_X.py    # 单文件
uv run pytest -k pattern         # 名字过滤
```

**任何 PR 前都得**:`uv run ruff check . && uv run pytest -q` 双绿。CI 也跑这两条。

## 核心契约(违反 = bug)

### 1. JSON envelope: **每条命令恰好一个**

所有 `qr` 子命令通过 [`quant_researcher/contract.py`](quant_researcher/contract.py) 的 `Envelope` 输出 **一个**信封到 stdout,exit code 0=ok / 1=error。lock-in 测试:`tests/test_cli.py::test_*_single_envelope*`。

### 2. `_emit` 在 try 内会双发信封 ⚠

`_emit(envelope)` 内部 `raise typer.Exit(code)` —— 而 `typer.Exit` 是 `Exception` 子类。所以:

```python
# ❌ 错。typer.Exit 被外层 except 抓住,再发一遍 failure envelope。
try:
    if bad:
        _emit(Envelope.failure(...))   # raises typer.Exit
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))       # 触发,导致双信封

# ✅ 对。验证 emit 放 try 外面;try 只包真正会业务异常的代码。
if bad:
    _emit(Envelope.failure(...))
try:
    do_db_work()
except Exception as exc:
    _emit(Envelope.failure(...))
else:
    _emit(Envelope.success(...))
```

MA-1 就踩过这个坑(MA-2 commit `39aeb44` 修)。新 CLI 命令必须照样写。

### 3. 子命令里 lazy import 重模块

`--help` 不应触发数据库 / FMP 模块加载。模式:

```python
@data_app.command("refresh")
def data_refresh(...) -> None:
    from quant_researcher.data.fmp import FMPClient        # lazy
    from quant_researcher.data.refresh import refresh_X    # lazy
    from quant_researcher.db import session_factory        # lazy
    ...
```

### 4. SQLAlchemy 模型注册 = side-effect import

`Base.metadata` 只见到被 import 过的模型类。`quant_researcher/db.py` 底部:

```python
# `Base` 已定义。下面这行触发模型类被读入。
from quant_researcher import models  # noqa: E402, F401
```

新模型 → `quant_researcher/models/X.py` → 在 `quant_researcher/models/__init__.py` `from ... import X` 并加进 `__all__`。**别忘了 `__init__.py`**,否则 `qr db init` 找不到你的表。

### 5. `known_at` 语义分裂(D6)

| Table | `known_at` 来源 | `server_default` |
|---|---|---|
| `securities`, `universe`, `profiles`, `daily_prices` | `func.now()`(ingestion ≈ 公开时间,足够) | ✅ |
| `income_statement`, `balance_sheet`, `cash_flow` | **解析自 FMP `acceptedDate`** —— D6 严格 | ❌(代码必填) |
| `financial_ratios`, `analyst_estimates` | `datetime.now(UTC)`(endpoint 不给 acceptedDate) | ❌(代码用 `now(UTC)`) |

测试锁:`tests/test_models.py::test_ma3_known_at_has_no_server_default`。**别给财报三表加 `server_default=func.now()`**,会破 point-in-time 查询。

### 6. Staleness 阈值(MA-4)+ refresh 默认 only-stale

阈值住在 [`quant_researcher/data/freshness.py`](quant_researcher/data/freshness.py) 的 `SCOPE_THRESHOLDS`,唯一来源:

| Scope | 阈值 | 判定字段 |
|---|---|---|
| `profile` | 30 天 | `MAX(known_at)` |
| `quote` | 3 calendar days | `MAX(trade_date)` — pragmatic Fri→Mon 安全,不引交易日历 |
| `financials` | 100 天 | `MAX(fiscal_date)` from `income_statement` —— "新季度落地了没",不是"最近刷过没" |
| `ratios` | 100 天 | `MAX(known_at)` |
| `estimates` | 7 天 | `MAX(known_at)` |

"是否过期"逻辑只走两个函数:`check_freshness(session, symbols)`(报告用)和 `stale_symbols(session, scope, symbols)`(filter 用)。**不要复制阈值或重新实现 staleness 查询**,所有路径必须经它俩。

`refresh_X(session, client, symbols, *, only_stale=True)` —— `only_stale=True`(默认)等价于在函数顶部跑一遍 `symbols = stale_symbols(session, "<scope>", symbols)`。CLI 层 `qr data refresh` 在 `--force` 缺省时按这个路径走;`--force` 时 CLI 自己用 `targets` 跳过 filter 并显式传 `only_stale=False`(避免函数层重做一次)。

### 7. Per-symbol AND per-period 失败隔离

`refresh_X(session, client, symbols, *, periods=...)` 单 ticker 任一 period 失败时:
- 该 period 的 FMP error 进 `SymbolOutcome.error`(带 `period:` 前缀)
- 其他 period / 其他 symbol 继续
- 该 symbol 整体 `ok=False`,但已经入库的部分**不回滚**

参考 `refresh_financials` 实现 + `tests/test_refresh.py::test_refresh_financials_isolates_per_*`。

## 文件地图

```
quant_researcher/
├── cli.py             所有 qr 子命令。lazy import,_emit 必须在 try 外。
├── config.py          pydantic-settings;DSN scheme 自动 normalize
├── contract.py        Envelope,改 schema 要升 SCHEMA_VERSION
├── db.py              Base + engine + session_factory + 底部 models side-effect import
├── universe.py        parse_watchlist_file (pure) + replace_universe (txn)
├── data/
│   ├── fmp.py         FMPClient(_get 内置限流+retry);加新 endpoint 走 _get_period_list
│   └── refresh.py     refresh_X 函数 + 共享 RefreshResult/SymbolOutcome + _as_* 解析助手
└── models/            每文件一/数个 model;__init__.py re-export = 注册
tests/                 mirror 上面结构;in-memory SQLite + MagicMock(spec=FMPClient)
docs/                  features.md + implementation-plan.md(改设计前先改这里)
config/watchlist.txt   .gitignored(用户机器上自填);.sample 是模板
.env.example           运行需要的 env 列表;.env 是 .gitignored
```

## 测试约定

- **DB**:in-memory SQLite。fixture pattern:
  ```python
  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite://", future=True)
      Base.metadata.create_all(engine)
      with Session(engine, future=True) as sess:
          yield sess
  ```
  CLI 测试用 `memory_db` fixture 把 `session_factory` patch 成 SQLite。
- **FMP 业务测试**(`test_refresh.py`):`MagicMock(spec=FMPClient)`,逐方法设 `return_value` / `side_effect`。
- **FMP HTTP 测试**(`test_fmp.py`):`respx.mock` + `httpx.Response(...)`。
- **TZ 坑**:SQLite 不存 tz。比较 `DateTime(timezone=True)` 列要用 `_naive_utc(dt)` 助手归一化(见 `tests/test_refresh.py`)。
- **CLI 测试**:`from typer.testing import CliRunner` + `_json_lines(output)` 解析多行 envelope,**断言只有 1 个**。
- **ruff `B008`** 已经 ignore(typer Option 默认值是文档化模式),别改回来。

## 加新功能 / 新里程碑的工作流(MA-1/2/3 已验证)

1. **先改 docs**:`implementation-plan.md` 里把里程碑展开成子任务;`features.md` 有新需求 → 加 D 编号。
2. **开 branch** `<milestone>`(例 `ma-4`),从 master。
3. **TaskCreate** 把子任务列上,逐项 `TaskUpdate in_progress → completed`。
4. **每一步**:写模型 / 函数 / CLI → 同步加测试 → `uv run ruff check . && uv run pytest -q` 必须双绿才能下一步。
5. **关键设计决策**在 docstring + 测试名里写清楚(参考 MA-3 `test_refresh_financials_known_at_equals_accepted_date`)。
6. **commit message** 用 `<milestone>: <一句话>`,body 列改动 + 测试数 + 设计决策。结尾固定 `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`。
7. **push + `gh pr create`**,PR body 用 MA-2/MA-3 PR 的格式(Summary / 设计决策 / Test plan / Out of scope)。
8. **用户跑 e2e**(真实 FMP + Supabase),通过后 merge。

## Schema 演进(D11:无 Alembic)

- **加新表**:`models/X.py` + `models/__init__.py` re-export → `qr db init` 自动落地。
- **加新列(可空)**:改 model → `qr db init` **不会**自动 ALTER 既有表。需要在 Supabase SQL Editor 手工 `ALTER TABLE X ADD COLUMN ...`。
- **改 / 删列**:Supabase dashboard 手工 SQL,顺序:先改 model + 跑测试 → 再 ALTER 生产库 → 部署。

## 常见坑

- **typer.Exit 是 Exception**:见上面 §2。
- **SQLite 无 tz**:用 `_naive_utc` 测试,生产 Postgres 没这问题。
- **FMP `acceptedDate` 不一定有**:`_as_datetime` 返回 None;`_ingest_statement` 会跳过 known_at=None 的行(避免 NOT NULL 违反)。
- **FMP 部分 endpoint 的 `period=quarter` 是付费**:用户跑出 402 → 加 `--periods annual` workaround,默认仍 `annual,quarter`。
- **`qr data refresh` 默认 only-stale(MA-4 破坏性变更)**:不带 `--force` 时,fresh 行会跳过 FMP 调用,envelope 里 `scopes.<scope>.skipped_fresh` 列出被跳过的票。若要复现 MA-3 之前"全刷"行为,加 `--force`。`refresh_X` 函数层的 `only_stale=True` 也是 default,改这行为时函数 + CLI 两侧都要同步。
- **`session.scalars(select(a, b, c))` 只返回第一列**;要多列 tuple 用 `session.execute(select(a, b, c))` 然后 `for row in result`。
- **`Base.metadata.create_all(checkfirst=True)` 不修改既有表**;不要指望它自动跟模型同步。
- **`pyproject.toml` 的 ruff `select = ["E", "F", "W", "I", "B", "UP"]`** + `ignore = ["B008"]`。别加 lint 规则没沟通。

## 调用约定(给协作 Claude 的元规范)

- **requirements-first**:用户偏好先讨论需求/方案再写代码。复杂改动**先 ExitPlanMode 让用户拍板**,别一上来就 Write/Edit。
- **Chinese-friendly**:用户用中文。回复用中文;代码 / commit / PR 标题用英文(国际可读)。
- **单 PR 一里程碑**(MA-2 / MA-3 都是 13 文件 / ~1200 LOC 单 PR,工作良好)。子任务用 TaskCreate 跟踪,不要拆成多 PR。
- **PR 前问用户**:push / 开 PR 之前**等用户跑 e2e 确认**(MA-1/2/3 都这样)。
- **改设计先改 docs**:不要在代码里偷塞决策。
