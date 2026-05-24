# CLAUDE.md

> 给 Claude Code(或任何 Claude 风格的 AI 协作者)看的工程手册。**先读这个再动代码。** 用户视角的 quick start 在 [`README.md`](README.md);设计决策在 [`docs/`](docs/)。

## 项目脉络(必读)

权威文档,改代码前先看:

- [`docs/features.md`](docs/features.md) — 需求 v1.0,**决策记录 D1–D11**。需求改了 → 这里加新 D。
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — 实现 v1.0,**I1–I8 + 里程碑 M0–MH**。实现策略变了 → 这里改。

代码现状:**M0 + MA(含 MA-5)+ MB + MC + ME(持仓部分)+ MD + MF + MH + MG 已建**(v1 八能力域全闭环)。**MA-5** `/key-metrics` 补全 ROE/ROA/fcf_yield(§6 末尾);**MH** 整包移植 quant-engine(§13);`qr morningcall`(§14)+ `qr earnings`(§15);`financial_ratios` 加 `roic` / `earnings_yield`;backtest 注册表 6 个单标的策略。**MG**(信号研究,本批):`quant_researcher/signals/`(factors 注册表 + panel + engine)+ `qr signal research/factors/list/runs/show`,算因子 IC/分位/衰减,落 `signals`/`signal_runs`(详见 §16)。

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

**MA-5:`refresh_ratios` 调两个 endpoint。** `ROE / ROA / fcf_yield` 不在 FMP `/ratios` 里(几乎总是 None),它们住在 `/key-metrics`。所以 `refresh_ratios` 每个 period 抓 `/ratios` **和** `/key-metrics`,用 `_key_metrics_by_date` 按 `fiscal_date` join,`_merge_key_metrics` 把这三个字段回填进 ratio 行 —— **仅当 /ratios 该字段为 None 时**(防御:万一 FMP 哪天在 /ratios 自己填了,以 /ratios 为准)。`/key-metrics` 失败(如付费 plan 不含 → 402)走 **per-period hard-fail**(symbol `ok=False`,错误带 `key-metrics:` 前缀),但 `/ratios` 行照常入库 —— 跟 §12 一致,**不**走 news 那种软失败,因为这三个是 MB 筛选的 first-class 字段。`/key-metrics` 还返回 `returnOnInvestedCapital` / `earningsYield` —— **已加列**(`return_on_invested_capital` / `earnings_yield`,screen 字段 `roic` / `earnings_yield`)。加这俩走的就是标准流程:`_KEY_METRIC_FIELDS` 加映射 + `_ratio_from_fmp` 加 None 占位 + model/screen 加列(`_merge_key_metrics` 是 generic 的,零改动)+ 手工 ALTER 生产库。要再加 /key-metrics 字段照抄即可。

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

### 11. MF 决策账本 — record / track / scorecard

**3 个入口** ([`quant_researcher/ledger/engine.py`](quant_researcher/ledger/engine.py))
- `record_decision(session, symbol, side, thesis, confidence, tags)` — 写 Decision 行 + 调 `research.bundler.bundle` 把当时仓库状态快照存进 research_bundles,`bundle_id` 入 Decision。`price_at_open` = `_price_at_or_before(symbol, opened_at)` —— **不**是 latest_close(latest_close 在测试里会拿到未来的 seed bar,实战可能拿到刚 ingest 的盘后 bar)。
- `track_decisions(session, as_of=None)` — for each Decision × 4 horizon(1w/1m/3m/6m),如果 `target_date <= as_of` 就计算 forward return + SPY return + sector return + alpha,`session.merge` 进 decision_tracking。**`session.merge` 关键**,re-run 同 horizon 覆盖不会冲突。
- `scorecard(session, group_by, horizon)` — 拉 Decision + tracking 行,Python 端按 group_by ∈ {confidence, sector, tag} 聚合,按 avg_alpha 降序返回。tag 是 list → 一条决策入 N 个 tag 组。

**关键设计点**
- **Alpha 计算**:`alpha = return − benchmark`,benchmark 用 sector ETF 优先(`sectors.etf_for_sector`),没匹配到 fall back 到 SPY。
- **Short 决策**:`side="sell"` 时 `return_pct = -(end/start - 1)`,股价跌 10% → +10%。
- **Price staleness window = 3 天**:`_price_near_date` 在 target_date ±3 天内没 bar 就返 None,避免拿月初的价格冒充月末(weekends+1 holiday 够用,长 gap 就是数据问题)。
- **Sector ETF mapping** 是硬编码常量([sectors.py](quant_researcher/ledger/sectors.py)),lowercase 匹配,缺失 fall back to SPY。FMP 的 sector strings 有变体(`"Financial Services"` vs `"Financials"`),映射表两个都收。

**注意:SPY 和 sector ETF 不是默认 universe 成员** —— 你得手动 `qr universe set` 把 SPY / XLK / XLE 等加进去然后 `qr data refresh --scope quote`,scorecard 才有 benchmark 数据。否则 alpha 列是 None。

### 12. Per-symbol AND per-period 失败隔离

`refresh_X(session, client, symbols, *, periods=...)` 单 ticker 任一 period 失败时:
- 该 period 的 FMP error 进 `SymbolOutcome.error`(带 `period:` 前缀)
- 其他 period / 其他 symbol 继续
- 该 symbol 整体 `ok=False`,但已经入库的部分**不回滚**

参考 `refresh_financials` 实现 + `tests/test_refresh.py::test_refresh_financials_isolates_per_*`。

### 13. MH 回测 — 整包移植 quant-engine + warehouse feed + 持久化

**`quant_researcher/engine/` 是 quant-engine 的整包移植**(verbatim,只改 `engine.*` → `quant_researcher.engine.*` import 前缀)。**改它前先想清楚是否要保持与上游可 re-sync** —— 大改动会让以后同步上游变难。已做的最小改动只有三处:① `data/data_feed.py` 删掉 `YFinanceFeed`(去 yfinance 依赖,留 `DataFeed` ABC + `CSVFeed`);② `analytics/metrics.py` 删掉 yfinance/matplotlib 版本上报;③ `engine.py` 的 `_fetch_spy_benchmark` 改为从注入的 `data_feed` 读 `benchmark_symbol`(原版用 yfinance 自动拉 SPY),并加 `verbose` 旗标(默认 True;CLI 路径传 `verbose=False` 静默 print,保 §1 单信封)。**丢弃**了 `export/ optimize/ data/cached_feed.py analytics/{chart,enhanced_charts,report}.py`(charting/QC/walk-forward 不在 v1)。risk/margin/stop 模块**移植了但 CLI v1 不接**(`risk_manager=None`)。

**qr 专属编排在 `quant_researcher/backtest/`**(不污染 engine 包,re-sync 友好):
- `engine/data/warehouse_feed.py` — `WarehouseDataFeed(DataFeed).fetch()` 读 `daily_prices` → `Bar`。**默认 `adjusted=True`**:用 `factor = adj_close/close` 把整根 OHLC 回调(split/dividend 正确),`close = adj_close`;`adj_close` 缺失 → factor=1;无 close 的行跳过。`--raw` 关掉。这是放在 engine/data/ 的唯一 qr 专属文件(additive,不与上游冲突)。
- `backtest/strategies/` — 内置策略注册表(`REGISTRY` dict,v1 有 6 个单标的:`sma_crossover` / `buy_and_hold` / `macd_crossover` / `bollinger_reversion` / `rsi_reversion` / `donchian_breakout`)。加内置策略:丢个 module + 在 `REGISTRY` 注册(keys 也驱动 CLI 错误里的 "valid:" 列表)。
- `backtest/loader.py` — `--strategy-file` 用 importlib 加载外部 `.py` 里的 `BaseStrategy` 子类(**本地执行,不沙箱** —— 跟本地跑任意脚本同信任级别)。
- `backtest/runner.py` — `run_backtest(...)` 唯一入口(CLI + Python 都走它)。解析策略(file 优先于 registry name)→ 单 symbol 策略自动注入 `symbols[0]` → 跑 `BacktestEngine(verbose=False)` → `calculate_metrics` → 写一行 `backtest_runs` → 返回 envelope-friendly summary(**不含** equity_curve/trade_log 大字段,那俩进 DB 由 `qr backtest show` 取)。
- **JSON 序列化两个坑**(都在 runner 处理):`calculate_metrics` 出 **numpy 标量** → `_to_jsonable` 转原生;`profit_factor` 等可能是 **inf/nan** → 转 None(Postgres JSONB 拒绝 Infinity)。新增写进 `backtest_runs` JSON 列的字段都要过 `_to_jsonable`/`_num`。

**依赖**:移植引入 `scipy`(metrics 的 PSR/skew/kurtosis)。**测试**:`tests/engine/` 是上游测试整包移植(235 个,验证移植正确性,改 import 即可);qr 专属在 `tests/test_warehouse_feed.py` / `test_backtest_runner.py` / `test_backtest_cli.py`。

**已知限制(upstream,v1 不碰、暂不修)** —— PR #6 review 标出,因偏离 upstream 风险大且 v1 用不到而**故意延后**(真要修先在 quant-engine 上游改再 sync):
- **多标的 bar 错位 → 前视偏差**(`engine.py` 事件循环):循环按 `range(max_bars)` 对每个 symbol 各自 `advance()`,**假设所有标的 bar 完全对齐**。若某标的有缺口(停牌/上市晚),它的 bar 会相对其他标的"左移",同一 `on_bar` 里看到不同日期的价格。v1 内置策略全是单标的(如 `sma_crossover`),且 warehouse 是美股共享交易日历(同期标的天然对齐),不触发;**多标的策略(走 `--strategy-file`)要自己保证标的历史齐全**。正解是按"全标的去重排序时间轴"迭代。
- **STOP_LIMIT 触发态不持久**(`broker.py` `_fill_stop_limit`):stop 触发后若当根 bar limit 没成交,只 return None 而**没转成 LIMIT 单**,下根 bar 重新判 trigger —— 价格回到 stop 另一侧会"un-trigger"。v1 只用市价单(`buy`/`sell`),不碰;用 `set_stop_loss` 等止损的自定义策略要注意。正解是触发后把 order 状态置为已触发并转 LIMIT。

### 14. `qr morningcall` — 组合晨报(features §E)

[`quant_researcher/research/morningcall.py`](quant_researcher/research/morningcall.py) `build_morning_call(session, *, account=None, as_of=None, news_per_holding=1)` 从 holdings + warehouse 拼一份**精简**组合晨报(**不是** N 份完整 bundle):逐持仓精简视图(权重/盈亏%/日涨跌/精简 ratios/估值 upside/1 条新闻/关联 decision)+ 组合层(总市值/总盈亏/sector exposure/top-bottom movers/现金)。**复用** bundler 的 `_latest_price` / `_latest_ratios` / `_recent_valuations` / `_recent_news` + `ledger.sectors.etf_for_sector`;profile/decision 批量查。`save_morning_call` 落 `MorningCallSnapshot`(uuid PK,`--save` 默认关)。**诚实 data 约定**:跨币种只 raw sum + note;现金取不到 → None + note;`day_change_pct` 是 close-to-close(只有日线,无隔夜 gap);空持仓 → ok + notes。估值 headline 取 dcf(没有 `"all"` 这种 model_type)。

### 15. `qr earnings` — 财报 actual-vs-est + 论点(features §D)

[`quant_researcher/research/earnings.py`](quant_researcher/research/earnings.py) `read_earnings(session, symbol, *, limit=4, transcript_excerpt=None, decision_limit=5)` 是**纯 warehouse 读**(不调 FMP、不写库;transcript 由 CLI 在线取后注入,跟 bundler 同款分离)。把最近 N 个 `IncomeStatement` actual 按共享 PK `(symbol, fiscal_date, period)` join `AnalystEstimate`,有估值就算 EPS/营收 surprise(`abs()` 分母防负估值翻号),论点只**陈列** Decision(不打分,Claude 判)。**关键 caveat**:估值是 forward + merge 覆盖的,过去期只有"当时 forward 时抓到"才有 → 历史 surprise **稀疏**;`estimate_available` / `estimates_matched` 把覆盖率摆明,绝不在没估值时暗示 beat/miss。`--transcript` 在线取(402-safe)。

### 16. MG 信号研究 — 因子 IC/分位/衰减(features §G)

[`quant_researcher/signals/`](quant_researcher/signals/) 三层:`factors.py`(`REGISTRY` 因子注册表)+ `panel.py`(仓库 I/O + 点位面板)+ `engine.py`(`run_signal` 唯一入口 + IC/分位/衰减数学 + 持久化)。`qr signal research --factor <name>` 在月度 rebalance 日给全 universe 按因子排名,量它对**前瞻收益**的预测力。

- **因子注册表**(`factors.py`):`fundamental` 复用 `screen.expression.FIELDS`(因子名 → financial_ratios 列)+ `price`(`momentum_12_1/6_1`、`reversal_1m`、`realized_vol_3m`,从 `PriceSeries` 算)。`direction`(±1/0)仅用于报告对齐多空,**不**翻原始 IC。
- **点位正确(PIT)是命根**(`panel.py`):基本面值走 `FinancialRatios → IncomeStatement` join 按 `IncomeStatement.known_at`(= 真 acceptedDate)`<= rebalance_date` 过滤 —— **不**能用 `FinancialRatios.known_at`(是 ingestion 时间,会泄漏未来)。价格因子只用 `<= anchor` 的 bar。前瞻收益用 calendar-day `HORIZON_DAYS`;动量用 trading-day 行偏移(252/126/21)。
- **效率**:`load_price_panel` 一次查询把每票价格序列灌进 numpy `PriceSeries`(`adj_close`、3 天 staleness),之后 forward-return/动量全是内存 bisect,不走 per-(symbol,date) 查询。
- **数学**(`engine.py`,全程过 `backtest.runner._to_jsonable` 防 numpy/inf/nan):IC = 每日 `scipy.stats.spearmanr`(**算前先 strip None/NaN 对**,constant 输入 → nan → 丢该日);summary 出 mean/std/IR/t-stat/hit-rate;分位 `argsort`+`array_split` 等量分桶 → 桶均收益 + 多空价差(raw + direction-aligned)+ monotonicity;衰减 = 各 horizon 的 mean IC。
- **诚实 coverage block**(必带):2 年价格 + 每股 ~2 个年报 → 基本面因子**准静态**(distinct 截面少、IC 自相关、t-stat 虚高)。`coverage.warnings` 在 fundamental quasi-static / n_dates<6 / avg_symbols<10 时明确警告 —— **绝不在没估值/样本薄时夸大 IC**。CLI 原样吐 `coverage`。
- **持久化**:`Signal`(定义)+ `SignalRun`(run_id uuid + ic_summary/quantiles/decay/coverage JSON),仿 Screen/ScreenRun。

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
├── engine/            MH:quant-engine 整包移植(core/data/execution/risk/indicators/
│                      strategy/analytics/engine.py)。改它前看 §13。warehouse_feed.py 是
│                      唯一 qr 专属新增文件
├── backtest/          MH:qr 编排层(runner.py 唯一入口 + strategies/ 注册表 + loader.py)
├── research/          bundler.py(数据包,§10)+ morningcall.py(§14)+ earnings.py(§15)
├── signals/           MG:factors.py(注册表)+ panel.py(PIT 面板)+ engine.py(IC/分位/衰减,§16)
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
8. **用户跑 e2e**(真实 FMP + Neon),通过后 merge。

## Schema 演进(D11:无 Alembic)

- **加新表**:`models/X.py` + `models/__init__.py` re-export → `qr db init` 自动落地。
- **加新列(可空)**:改 model → `qr db init` **不会**自动 ALTER 既有表。需要在 Neon console 的 SQL Editor 手工 `ALTER TABLE X ADD COLUMN ...`。例:`financial_ratios` 的 `return_on_invested_capital` / `earnings_yield` 就是这么加的(`ALTER TABLE financial_ratios ADD COLUMN return_on_invested_capital double precision; ADD COLUMN earnings_yield double precision;`),加完 `qr data refresh --scope ratios --force` 回填。
- **改 / 删列**:Neon console 手工 SQL,顺序:先改 model + 跑测试 → 再 ALTER 生产库 → 部署。

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
- **最小 diff(surgical)**:只改任务要求的行 —— 每行改动都应能追溯到需求。别顺手"优化"邻近代码 / 注释 / 格式,别重构没坏的东西,**match 既有风格**(移植代码尤其,如 `engine/` 与上游对齐)。只清理**你自己**改动产生的 orphan(unused import/var);发现既有 dead code → 提一句,别擅自删。无关的改动**另开 PR**,别塞进当前里程碑。
- **简单优先**:解决问题的最小代码,不写需求外的功能 / 不发生需求的"灵活性"或抽象 / 不为不可能的场景加 error handling。写完自问"资深工程师会不会觉得这过度设计了?"。
