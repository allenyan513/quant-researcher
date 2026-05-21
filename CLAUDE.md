# CLAUDE.md

> 给 Claude Code(或任何 Claude 风格的 AI 协作者)看的工程手册。**先读这个再动代码。** 用户视角的 quick start 在 [`README.md`](README.md);设计决策在 [`docs/`](docs/)。

## 项目脉络(必读)

权威文档,改代码前先看:

- [`docs/features.md`](docs/features.md) — 需求 v1.0,**决策记录 D1–D11**。需求改了 → 这里加新 D。
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — 实现 v1.0,**I1–I8 + 里程碑 M0–MH**。实现策略变了 → 这里改。

代码现状:**M0 + MA + MB + MC + ME(持仓部分)+ MD 已合并 master**,下一里程碑是 **MF**(决策账本)。ME 的 morningcall 数据包延后到 MF 或后续。

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

### 7. MB 筛选 — AST sandbox + 命名 DSL

**基本面表达式** ([`quant_researcher/screen/expression.py`](quant_researcher/screen/expression.py)) 用 `ast.parse(..., mode='eval')` 把字符串解析成 AST,**手工 walk**,**绝不调 `eval`**。允许节点白名单:`BoolOp(And|Or)` / `UnaryOp(Not|USub|UAdd)` / `Compare` / `Name` / `Constant` / `List` / `Tuple`。Call / Attribute / Subscript / Lambda / 推导式全部拒绝。新加字段必须进 `FIELDS` 注册表(同时也是错误消息里的"valid:"列表)。

**技术 DSL** ([`quant_researcher/screen/technical.py`](quant_researcher/screen/technical.py)) 是 `name[arg1,arg2]` 形式,逗号分隔,所有 predicate AND。Predicate 注册表在文件底部 `_REGISTRY`,加新 predicate 时写 factory 函数返回 `Predicate = Callable[[closes, volumes], bool]`。Parser 处理 `[…]` 内嵌逗号(depth 跟踪)。

**State 加载** ([`quant_researcher/screen/engine.py`](quant_researcher/screen/engine.py)) 一次查询每张源表,Python 端按 symbol 聚合(简化 greatest-N-per-group)。规模到 300 票 × ~10 annual ratios = 3k 行,O(N) Python 完全够。MD 起若增加因子要在 SQL 端做窗口函数,届时重写 `build_symbol_state`。

**新增字段流程**:加 column → 加进 `FIELDS` 注册表 → 在 `build_symbol_state` 写填充逻辑 → 加测试 → 文档同步。

### 8. MC 估值 — 模型分层 + 快照可复现

**层次**([`quant_researcher/valuation/`](quant_researcher/valuation/))
- `wacc.py` —— CAPM + Bloomberg adjust(`2/3·β + 1/3`)。v1 不算债务结构(简化到 cost-of-equity),改时增加 `cost_of_debt` / `tax_rate` / `debt_weight` 参数即可,DCF 仍接 WACC 标量。
- `helpers.py` —— 只读 accessor:`historical_fcf` / `net_debt` / `shares_outstanding`(`net_income/eps_diluted` 推) / `sector_peer_median` / `earnings_growth_rate`。所有方法在数据缺失时返回 None,上层判断。
- `dcf.py` —— 纯函数 `dcf_fcff` + `sensitivity_5x5`,无 DB 依赖,unit-testable。Terminal value 只有 Gordon,未来加 exit-multiple 走 `terminal_method` 参数。
- `peg.py` / `multiples.py` —— 模型层,各自接受 session 拉一次数据然后计算。
- `engine.py` —— `value_company` 是唯一对外入口;CLI 和未来 Python 调用都走这里。每模型写一行 `valuation_snapshots`(JSON `assumptions` + `result` + `sensitivity`),`code_version` 自动写入,replay 可对齐。

**约定**
- WACC ≤ terminal_growth 时 `dcf_fcff` 抛 `DCFError`(避免 Gordon 除零)。`sensitivity_5x5` 在 grid 里把这种 cell 写成 `None`,而不是抛。
- 缺数据时 `value_company` 不抛 —— 返回 `models["dcf"]["fair_value_per_share"] = None` + `"note": "..."`,保持 envelope 一致 ok=true。这样 Claude 拉多票时单票坏数据不会废掉整批。
- 假设覆盖(`assumptions` dict)的 keys 与 `dcf_fcff` 参数 1:1 对应 —— 改名要同步两个地方。
- 同业中位数即时算,不缓存。如 MG 起需要历史稳定 sector beta,加 `sector_betas` 表;v1 不需要。

### 9. ME 持仓 — Flex API 两步走 + 统一 importer

**Flex 两步流程** ([`quant_researcher/holdings/ibkr_flex.py`](quant_researcher/holdings/ibkr_flex.py))
1. `SendRequest?t=...&q=...&v=3` → `<FlexStatementResponse>` 含 `ReferenceCode`。
2. 轮询 `GetStatement?t=...&q=<ref>&v=3` —— 还没生成好时 IBKR 返回 `ErrorCode 1019`(Status=Warn,继续轮询);生成完返回 `<FlexQueryResponse>`,我们解析 `<OpenPositions><OpenPosition .../></OpenPositions>`。`max_poll_attempts=6` × `poll_delay=8s` 默认能 cover 大部分实盘 query。

**Schema 探查**:Flex Query 的字段取决于用户在 IBKR 后台勾了什么。ME-1 用 user 的真 token 拉过一次确认 `position` / `markPrice` / `costBasisPrice` / `fifoPnlUnrealized` / `percentOfNAV` / `accountId` / `reportDate` 都在。改 Flex Query 的 columns 后 importer 自动跟得上(`raw` JSON 保留全部 attrs)。

**统一 importer** ([`holdings/importer.py`](quant_researcher/holdings/importer.py))
- `import_holdings(session, source="flex"|"csv"|"manual", payload, ...)`,内部转译到统一 `Holding` 字段。
- 用 `session.merge` —— PK `(account_id, symbol, as_of_date)` 相同时覆盖(同一天再跑 sync 会更新 markPrice 不会冲突)。
- 单条数据缺 PK 字段进 `result.skipped`,不阻塞其他行。

**CSV 格式**:必需 `account_id, symbol, quantity, as_of_date`(YYYY-MM-DD);可选 `avg_cost / mark_price / market_value / currency / asset_category / side / description`。空数字 cell 入 None。

**坑提醒**:
- OPT 持仓的 `symbol` 是 OCC 风格(例 `"META  260821P00530000"`,中间双空格),不要 trim/拆分,原样存。
- `position` 可以是负数(short),`side` 同步写 "Short"。
- Flex `reportDate` 是 `YYYYMMDD`(无连字符),`_parse_flex_date` 处理。
- token 别提交进 repo,只走 `.env`。

### 10. MD 研究数据包 — bundler + news + FMP 402 软失败

**bundler** ([`quant_researcher/research/bundler.py`](quant_researcher/research/bundler.py)) 是纯 DB 聚合器 —— 不调 FMP,只读 warehouse。`build_bundle(session, symbol)` 走 9 个 section helper(`_profile_section` / `_latest_price` / `_latest_ratios` / `_recent_statements` × 3 / `_forward_estimates` / `_recent_valuations` / `_holdings_section` / `_recent_news`),每个 helper 数据缺失返回 None / []。`bundle(...)` 在 build_bundle 之上加持久化到 `research_bundles`。

**FMP 402 软失败** ([`quant_researcher/data/fmp.py`](quant_researcher/data/fmp.py) `get_news` / `get_earnings_transcript`):用户 plan 不包含 premium 端点时 FMP 返 402 —— 这两个方法 catch FMPError(status_code=402) 返 []。MA-3 的财报方法仍 raise,因为它们是 first-class 数据(MD 的 news 是 nice-to-have)。

**news 表 dedup** ([`research/refresh.py`](quant_researcher/research/refresh.py)):PK 是 `(symbol, published_at, url)`。tuple 比对前用 `_key()` 把两边的 tz-aware datetime 都 strip 成 naive UTC,因为 SQLite 读出来的 `DateTime(timezone=True)` 列是 naive,Postgres 是 aware。

**transcript_excerpt 是 caller-provided**:bundler 不主动调 FMP `/earning-call-transcript`(那个 endpoint 很大,2000 字 truncate 后还是几 K 字符)。`qr research bundle` v1 不传 transcript,留个 hook。后面如果要做 earnings-read 单独命令(`qr research earnings SYM`)再决定要不要主动拉。

### 11. Per-symbol AND per-period 失败隔离

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
