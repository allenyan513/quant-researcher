# quant-researcher — 实现规划 v1.0

> 承接 `docs/features.md`(需求 v1.0,D1–D11) · 本文定实现 · 2026-05-19

## 1. Context

`features.md` 定了"做什么"(A–H 八个能力域、D1–D11)。本文定"怎么做":接口形态、数据层、仓库 schema、复用映射、按 D4 顺序的里程碑、验证方法。目标是给后续编码一份可直接执行的蓝图。

## 2. 实现决策(I1–I8)

- **I1 接口 = CLI 优先,JSON 契约。** 单一可执行 `qr`,子命令即能力原语;stdout 输出稳定 JSON 信封。Claude Code 经 Bash 组合调用。后续若需要,MCP 可作薄适配层包同一核心库(v1 不做)。
- **I2 数据层 = SQLAlchemy 2.x(声明式 model)+ Supabase Postgres(用户自建独立项目,与 valuescope 隔离)。无 Alembic、无 docker-compose(D11)。** Schema 由 `Base.metadata.create_all(checkfirst=True)` 经 `qr db init` 幂等应用;演进=手写 SQL(Supabase dashboard)或新增 model 后重跑 `init`(仅添加表/列安全;改/删需手工 ALTER)。schema 形态照搬 valuescope 成熟设计(D1:模式参考)。`QR_DATABASE_URL` 仍是任意 Postgres DSN,但 v1 不文档化备选后端。Pro tier 已用,无 free-tier 暂停问题。
- **I3 节奏 = 逐域做扎实,D4 顺序** A → B → C →(D/E/F)→ G → H,每域达可用再下一个。
- **I4 复用 quant-engine = 移植其紧依赖簇**(指标 + 回测引擎 + 分析)进本仓库,命名空间化,不作硬依赖/子模块(D5)。
- **I5 复用 valuescope = 仅模式参考**(估值数学、仓库 schema、分层刷新编排),Python 重写,不引用其代码/DB(D1)。
- **I6 工具链 = Python 3.13 + uv**(本机已确认;无 poetry/pyenv)。**Supabase 项目用户自建**(单独项目,Pro tier);无本地 Postgres / docker compose(D11)。
- **I7 配置/密钥(D8)** = `.env`(git-ignored)+ `.env.example`;watchlist 走 `config/watchlist.txt`(git-ignored,附 `.sample`)。env 名:`QR_DATABASE_URL`、`FMP_API_KEY`、`FRED_API_KEY`(可选)、`FLEX_TOKEN_KEY`/`FLEX_QUERY_ID_LIVE`(E 阶段)。
- **I8 边界** = 后端只供结构化数据;Notion/排版/叙述交给 Claude 与现有 skills(features.md 既定边界)。后端不写 Notion。

> 已对 features.md 追加 **D9 + D10 + D11**:D9 修订设计原则 3「本地、可离线」→「自有仓库,存储后端 DSN 可配置」并记录实现期决策;D10 默认 Supabase Postgres(独立项目,与 valuescope 隔离);**D11**(最终)精简 DB 工具链——取消 Alembic 与 docker-compose,schema 走 `Base.metadata.create_all` via `qr db init`;Supabase Pro 已购,无暂停顾虑。

## 3. 架构总览

```
Claude Code ──Bash──> `qr <verb> ...` (CLI 前端,argparse/typer)
                          │  解析 → 调核心库 → 输出 JSON 信封
                          ▼
        quant_researcher/ (核心库,纯 Python)
        ├─ config        env/DSN/watchlist 加载
        ├─ db            SQLAlchemy declarative models + create_all(schema 仿 valuescope)
        ├─ data          FMP REST client + 分层刷新编排(仿 update-ticker/update-all)
        ├─ screen        统一筛选引擎(衍生条件 + 技术信号扫描)
        ├─ valuation     DCF-FCFF/PEG/multiples/EPV/DDM + WACC(数学仿 valuescope)
        ├─ research      深度数据包 / 财报速读 聚合器
        ├─ holdings      IBKR Flex(Python 重写)/ CSV 持仓
        ├─ ledger        决策账本 + 远期收益跟踪 + 计分卡
        ├─ signals       因子/信号研究(IC、分位)
        ├─ engine/       【移植自 quant-engine】core/strategy/portfolio/
        │                indicators/data/execution/analytics/engine.py
        ├─ snapshots     通用不可变输入快照(内容哈希,可复现)
        └─ contract      JSON 信封 + schema 版本
```

**JSON 信封(Claude 契约,所有命令统一)**
```json
{ "ok": true, "schema_version": "1", "as_of": "2026-05-19",
  "data_freshness": {"prices":"2026-05-19","financials":"2026-Q1"},
  "snapshot_id": "sha256:…", "code_version": "git:…",
  "data": { /* 命令结果 */ } }
```
错误:`{ "ok": false, "error": {"code":"…","message":"…"} }`。

## 4. 仓库目录

```
quant-researcher/
├─ docs/{features.md,implementation-plan.md}
├─ pyproject.toml            (uv 管理)
├─ .env.example
├─ config/watchlist.sample.txt
├─ quant_researcher/         (上述核心库 + engine/ 移植)
├─ tests/                    (移植 quant-engine 测试 + 新增)
└─ qr  (入口: python -m quant_researcher.cli)
```

## 5. CLI 命令面(= 能力契约)

| 域 | 命令 | 输出 |
|---|---|---|
| A | `qr db status|init|ping` · `qr data refresh [--scope all\|prices\|financials\|estimates\|profile] [--ticker SYM]` · `qr data freshness` · `qr universe set\|show` | 刷新结果 / 新鲜度 |
| B | `qr screen run --expr "marketCap>10e9 AND pe>5 AND pe<30 AND forwardPe<pe" [--technical "macd_golden_cross within 5d"] [--save NAME]` · `qr screen list\|show\|diff NAME` | 标的列表(可排序、带 as_of、可 diff) |
| C | `qr value TICKER [--models dcf_fcff,pe,peg,epv,ddm] [--assume growth=…,wacc=…]` · `qr value history TICKER` | 合理价值区间 + 敏感性 + snapshot_id |
| D/#6 | `qr research bundle TICKER` · `qr earnings read TICKER` | 结构化数据包 / 财报 actual-vs-est + 论点偏离 |
| E | `qr holdings load [--source flex\|csv --path …]` · `qr morningcall` | 逐持仓 + 组合数据包(Claude/skill 渲染→Notion) |
| F | `qr ledger add TICKER --action buy\|sell\|trim\|add\|avoid --thesis … --conviction N --source …` · `qr ledger track` · `qr ledger scorecard` | 决策入账(快照当时数据)/ 1w-6m 超额 vs SPY+行业ETF / 战绩 |
| G | `qr signal research --spec PATH` · `qr signal list\|show ID` | 因子 IC/分位/衰减 + 版本化信号 |
| H | `qr backtest run --strategy PATH [--symbols … --start --end]` · `qr backtest list\|show ID` | 指标/成交日志/净值曲线 + run_id |

## 6. 仓库 Schema(仿 valuescope + qr 专属)

**仿 valuescope(数据)**:`securities`(master) · `profiles`(FMP /profile) · `income_statement` / `balance_sheet` / `cash_flow`(三大报表,**`known_at` = FMP `acceptedDate` 公布日,实现 D6 务实时点**) · `financial_ratios`(MA-3,`known_at`=now,务实让步) · `daily_prices` · `analyst_estimates` · `price_target_consensus`(后) · `sector_betas`(后) · `earnings_events`(后)。
**qr 专属(产出,皆带 `params` JSONB + `input_snapshot_id` + `code_version` + `model_version`,实现可复现)**:`screens`(定义) · `screen_runs`(结果快照) · `valuation_snapshots` · `research_bundles` · `signals`/`signal_runs` · `decisions`/`decision_tracking` · `backtests`/`backtest_runs` · `snapshots`(通用不可变输入快照,内容哈希)。

## 7. 里程碑(D4 顺序,逐域扎实)

- **M0 脚手架** ✅(2026-05-19):uv 工程、`qr` 入口(typer)、JSON 信封/契约、config(pydantic-settings 读 `QR_DATABASE_URL`)、SQLAlchemy `Base`、`qr db status|init|ping`、单元测试(envelope + CLI smoke)、ruff + GitHub Actions CI。
- **MA 仓库+数据(A)**:SQLAlchemy 声明式 model(`Base.metadata.create_all` via `qr db init`);FMP REST client(令牌桶~250/min);分层刷新(quote 日 / 财报事件驱动 / estimates 周五 / profile 月,幂等 upsert,仿 `update-ticker`+`update-all`);`known_at`/`as_of`;`qr data refresh/freshness`、`qr universe`。
  - **MA-5 key-metrics 补全**(2026-05-21):`refresh_ratios` 接上 FMP `/key-metrics`,补全 `/ratios` 缺的 `ROE / ROA / fcf_yield`(之前永远 None,导致 `qr screen --fundamental "roe>0.15"` 静默 0 命中)。按 `fiscal_date` join,仅回填 None 字段,`/key-metrics` 402 走 per-period hard-fail(详见 CLAUDE.md §6 末尾)。`returnOnInvestedCapital` / `earningsYield` 需 ALTER 加列,留 MG。
- **MB 筛选(B)**:表达式解析(衍生比较 `forwardPe<pe`、分位);技术扫描复用移植指标(MACD 金叉/均线/RSI/52w/放量,"近 N 日"窗口);命名/持久化/diff。
- **MC 估值(C)**:WACC(Bloomberg 调整 β + sector_betas + FRED/兜底 4.5%);DCF-FCFF(增长/EBITDA 退出)、PEG、P/E·EV/EBITDA·EV/Rev 倍数、EPV、DDM;5×5 敏感性;假设可覆盖;快照持久化。
- **MD/ME/MF**:`research bundle`/`earnings read` 聚合器(含 insider/13F/analyst/transcript)→ 数据包+快照;`holdings`(先 CSV,后 IBKR Flex Python 重写)+ `morningcall` 数据包;决策账本(入账即快照当时数据 + track 1w/1m/3m/6m vs SPY+行业ETF + scorecard 按论点/行业/信心)。
- **MG 信号(G)**:假设规格 → 宇宙+历史算因子 → IC/分位/衰减 → 版本化信号(反哺 B/F/H)。
- **MH 回测(H)** ✅(2026-05-22):quant-engine **整包移植**到 `quant_researcher/engine/`(38 文件,改 import 前缀;丢弃 export/optimize/charts/cached_feed;去 yfinance);`WarehouseDataFeed(DataFeed).fetch()` 读 `daily_prices`(默认 adj_close 回调 OHLC);`quant_researcher/backtest/`(runner + 策略注册表 + `--strategy-file` loader);`qr backtest run/list/show` → 指标/成交/净值 + `backtest_runs` 持久化。risk/margin 移植但 CLI v1 不接。新依赖 scipy。上游 235 测试整包移植 + 21 qr 专属测试。详见 CLAUDE.md §13。

## 8. 复用映射(关键路径)

**移植自** `/Users/alin/Github/quant-engine`(→ `quant_researcher/engine/`,~25 文件):`engine/core/`、`engine/strategy/base.py`、`engine/portfolio/`、`engine/indicators/`(纯 numpy:sma/ema/macd/rsi/atr/bollinger/donchian)、`engine/data/data_feed.py`(`DataFeed.fetch()` 抽象)、`engine/execution/`、`engine/analytics/metrics.py`(`calculate_metrics`)、`engine/engine.py`(`BacktestEngine`)、`tests/`。风险/保证金模块可省(`risk_manager=None`)。新增 `WarehouseDataFeed` 实现 `fetch(symbol,start,end)->list[Bar]`。
**模式参考** `/Users/alin/Github/valuescope`(不引用代码):`src/lib/valuation/*.ts` → `quant_researcher/valuation/*.py`;`dcf-helpers.ts`/`wacc.ts` → WACC/折现工具;`src/lib/data/update-ticker.ts`+`scripts/update-all.ts` → `data/refresh.py`;`src/lib/db/schema.sql` → SQLAlchemy 声明式 model(`quant_researcher/db.py` 中 `Base` 子类);`src/lib/data/brokers/ibkr-flex.ts` → `holdings/ibkr_flex.py`(ME 阶段)。FMP base `https://financialmodelingprep.com/stable`;端点:quote / income|balance|cash-flow-statement / ratios / analyst-estimates / price-target-consensus / profile / stock-peers / earnings-calendar / company-screener。

## 9. 验证

- **M0** ✅:`uv sync`;`qr --help`;ruff clean;pytest 7/7 通过;DB 侧由用户跑 `qr db ping && qr db init && qr db status`。
- **MA**:`qr universe set --file config/watchlist.sample.txt`;`qr data refresh --scope all`;`qr data freshness` 显示覆盖;抽查某票 financials 与 FMP 一致;`known_at` 正确。
- **MB**:`qr screen run --expr "marketCap>10e9 AND pe>5 AND pe<30 AND forwardPe<pe"` 结果合理;`--technical "macd_golden_cross within 5d"` 命中;刷新后 `qr screen diff` 显示进出。
- **MC**:`qr value AAPL` 合理价值与公开估值量级一致;敏感性矩阵存在;snapshot 可复现(同输入→同值)。
- **MD/E/F**:数据包通过 schema 校验;`ledger add → track → scorecard` 超额计算对账 SPY+行业ETF。
- **MG**:已知因子(如动量)IC/分位输出合理。
- **MH**:移植 `pytest` 全绿;`WarehouseDataFeed` 与 `CSVFeed` 同数据回测结果一致;`qr backtest run` 产出标准指标。
- **契约**:每命令输出合法信封,`--json` 可解析,`snapshot_id` 可解引用。

## 10. 非目标(v1)

实盘下单 / 盘中·分钟·期权·期货 / 多用户鉴权 / 后端做 NLP 或写 Notion / MCP·REST 前端(留后) / 全 ~8000 宇宙(v1 仅 watchlist ~200-300)。
