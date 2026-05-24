# quant-researcher

> Claude Code 编排的美股投研底座 —— 提供"取数 + 计算 + 持久化 + 可复现",叙述交给 Claude。

[![CI](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml/badge.svg)](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml)

## 状态

**v1 八能力域全闭环,M0 + MA(含 MA-5)+ MB + MC + MD + ME + MF + MG + MH 已落地(2026-05-23)。** 目前可用能力:数据库脚手架、Watchlist、FMP 客户端、profile/OHLCV/三大报表/比率/估计刷新、freshness 报告、只刷过期数据、**筛选**(基本面 AST + 技术 DSL)、**估值**(DCF/PEG/倍数)、**Flex/CSV 持仓快照**、**研究数据包**、**组合晨报**、**财报速读**、**决策账本(snapshot + 1w/1m/3m/6m 跟踪 vs SPY/sector ETF + scorecard)**、**信号研究**(因子 IC/分位/衰减)、**回测**(移植 quant-engine,6 个内置策略)。路线图见 [`docs/implementation-plan.md`](docs/implementation-plan.md) §7。

## 这是什么

我有一套围绕美股的个人投研工作流,但散在 `quant-engine`(回测)、`valuescope`(估值 SaaS)、若干 Claude skills(盘前/盘后/深度调研/周报)、FMP MCP 之间。Claude Code 没法把"一句自然语言"顺畅地拆成跨能力的研究链路。

quant-researcher 就是这个串接层 —— 给 Claude Code 用的辅助投研底座:

- **CLI 是唯一接口**(`qr ...`),stdout 输出**稳定 JSON 信封**。Claude Code 经 Bash 调度,在 envelope 间自由组合。
- **自有数据仓库**(Neon Postgres;`QR_DATABASE_URL` 接任意 Postgres DSN),从 FMP 拉数据落地,支持 D6 point-in-time(财报 `known_at` 用 FMP `acceptedDate`)。
- **可复现**:每条结果都带 `as_of` / `data_freshness` / `code_version` / 可选 `snapshot_id`。
- **personal-first**:为我自己的 IBKR + Notion + Claude skills 工作流定型。开源但不为通用性牺牲顺手程度。

详细需求见 [`docs/features.md`](docs/features.md)(D1–D11 决策记录);实现蓝图见 [`docs/implementation-plan.md`](docs/implementation-plan.md)(I1–I8 + 里程碑 M0→MH)。

## 快速开始

需要:Python 3.13+、[uv](https://docs.astral.sh/uv/)、Neon Postgres 项目(或任意 Postgres DSN)、[FMP](https://financialmodelingprep.com) API key。

```bash
git clone git@github.com:allenyan513/quant-researcher.git
cd quant-researcher
uv sync                                                 # 安装依赖到 .venv

cp .env.example .env                                    # 填 QR_DATABASE_URL + FMP_API_KEY
$EDITOR .env

uv run qr db ping                                       # 验证连接
uv run qr db init                                       # 应用 schema (21 张表)
uv run qr db status                                     # 查看建出来的表

cp config/watchlist.sample.txt config/watchlist.txt     # 自定义关注池
uv run qr universe set --file config/watchlist.txt      # 入库
uv run qr universe list

uv run qr data refresh --scope all                      # 首次:全 missing → 全刷
uv run qr data freshness                                # 看每 scope 哪些票过期/缺失
uv run qr data refresh --scope all                      # 第二次:fresh 自动跳过
uv run qr data refresh --scope profile --force          # 强制全刷某 scope
```

如果你的 FMP 订阅不含某 endpoint 的季度数据,加 `--periods annual` 跳过 quarter。

### 新鲜度与刷新(MA-4)

`qr data refresh` **默认只刷"过期或缺失"** 的票,不再无脑全刷(MA-3 之前的行为)。每个 scope 有硬编码阈值:

| Scope | 阈值 | 判定字段 |
|---|---|---|
| profile | 30 天 | `known_at` |
| quote | 3 天 | `trade_date` |
| financials | 100 天 | `fiscal_date`(看"新季度落地了没") |
| ratios | 100 天 | `known_at` |
| estimates | 7 天 | `known_at` |

`qr data freshness` 输出每 scope 的 `{total, fresh, stale, missing, stale_symbols}`,Claude 可以这样自动驱动刷新:

```bash
STALE=$(uv run qr data freshness --scope quote | jq -r '.data.scopes.quote.stale_symbols | join(",")')
[ -n "$STALE" ] && uv run qr data refresh --scope quote --symbols "$STALE"
```

要强制刷新所有票(不管 fresh),加 `--force`。阈值定义在 [`quant_researcher/data/freshness.py`](quant_researcher/data/freshness.py)。

### 筛选(MB)

**基本面表达式**用 Python 语法,但只走安全的 AST 子集(comparisons + and/or/not + names + constants)—— 不调 `eval`,拒绝 Call/Attribute/Subscript。允许的字段见 `qr screen fields`(profile / 最新 annual ratios / 最新 close)。

```bash
# PE 低、PEG 合理、科技行业
uv run qr screen run --expr "pe < 30 and peg < 1.5 and sector == 'Technology'"

# 价值味更重 + 现金流好
uv run qr screen run --expr "pb < 3 and fcf_yield > 0.05 and debt_equity < 1.0"

# 行业过滤 + IN list
uv run qr screen run --expr "sector in ['Technology', 'Energy'] and roe > 0.15"
```

**技术扫描**是命名 predicate DSL(逗号分隔,所有 AND):

```bash
# 趋势:在 200 日均线之上 + 最近 5 日 MACD 金叉
uv run qr screen run --technical "above_sma[200],macd_golden_cross[5]"

# 超卖买入:近 3 日 RSI 跌破 30 + 接近 52 周低点
uv run qr screen run --technical "rsi_oversold[3],near_52w_low[5]"

# 异动:成交量比 20 日均量 2 倍
uv run qr screen run --technical "volume_spike[20,2]"
```

两者可以**叠加**(都是 AND):

```bash
uv run qr screen run \
  --expr "pe < 30 and fcf_yield > 0.04" \
  --technical "above_sma[200]" \
  --name "growth_value_in_uptrend"
```

每次 run 都进 `screen_runs` 表(envelope 返回 `run_id`),`qr screen diff --from R1 --to R2` 对比两次结果。

### 估值(MC)

```bash
# 默认跑 DCF + PEG + 行业倍数,持久化到 valuation_snapshots
uv run qr value AAPL
#    envelope.data.fair_value_per_share_mean = (DCF + PEG + multiples) / N
#    envelope.data.upside_pct_mean = mean / current_price - 1
#    envelope.data.snapshot_ids = {dcf: uuid, peg: uuid, multiples: uuid}

# 单一模型
uv run qr value AAPL --model dcf
uv run qr value AAPL --model multiples

# DCF 假设覆盖(JSON;支持 growth_rate / terminal_growth / wacc / n_years / rf / erp / base_fcf)
uv run qr value AAPL --model dcf \
  --assumptions '{"growth_rate": 0.10, "terminal_growth": 0.03, "wacc": 0.09}'
```

**模型逻辑**:
- **DCF-FCFF**:用过去 5 年 FCF 推中位 + CAGR(夹在 ±10%/20%),Gordon 终值;5×5 敏感性 grid 覆盖 (growth ± 4pp) × (WACC ± 2pp)。EBITDA 退出倍数留 TODO。
- **WACC**:Bloomberg 调整 β(`2/3 × β + 1/3`)+ CAPM(默认 RF=4.5%、ERP=5.5%);v1 不做债务结构调整,等价于 cost-of-equity。
- **PEG**:Lynch 公平 P/E ≈ 年增长率(%)。从 5 年 net_income CAGR 推。
- **行业倍数**:P/E、EV/EBITDA、P/S 的同行业中位数 × 公司当期指标 → 隐含价值。
- **EPV/DDM 延后**(同 schema 直接加)。

每次 `qr value` 写 N 行(每模型一行)到 `valuation_snapshots`,JSON `assumptions` + `result` + `sensitivity` 完整可复现。

### 持仓(ME)

两种来源:
- **IBKR Flex Query API**(推荐) —— 需要在 IBKR 后台建好 Flex Query 并把 `FLEX_TOKEN_KEY` / `FLEX_QUERY_ID_LIVE` 填到 `.env`。`qr holdings sync` 两步走(SendRequest 拿 reference,GetStatement 轮询直到生成完),自动解析 XML,upsert 到 `holdings`。
- **CSV** —— `qr holdings import-csv --file path.csv`。必需列 `account_id, symbol, quantity, as_of_date`,可选 `avg_cost, mark_price, market_value, currency, asset_category, side, description`。

`holdings` 表 PK 是 `(account_id, symbol, as_of_date)`,所以**每天一份快照**自动累积 —— 同一 PK 重跑会覆盖(merge),不同 `as_of_date` 累加。`qr holdings history --symbol AAPL` 走时间线。

```bash
uv run qr holdings sync                                              # 从 Flex 拉一次
uv run qr holdings list                                              # 默认每 (账户, 票) 最新
uv run qr holdings list --as-of 2026-05-20                           # 特定日期
uv run qr holdings history --symbol AAPL --limit 30                  # 单票回看 30 个快照
```

## 命令速查

| 命令 | 作用 |
|---|---|
| `qr db ping` | `SELECT 1`,延迟 + Neon scale-to-zero 唤醒用 |
| `qr db init` | `Base.metadata.create_all`(幂等,不改既有列) |
| `qr db status` | 显示 server_version、expected/present/missing 表 |
| `qr universe set --file PATH` | 用文件替换 universe 表 + upsert securities |
| `qr universe list [--limit N]` | 打印当前 universe |
| `qr data refresh --scope <X>` | `X ∈ {profile, quote, financials, ratios, estimates, all}`;**默认只刷过期/缺失** |
| `qr data refresh ... --force` | 关掉 freshness filter,强制全刷 |
| `qr data refresh ... --symbols A,B,C` | 限定子集(默认全 universe) |
| `qr data refresh ... --periods annual,quarter` | 财报/比率/估计的 period 过滤 |
| `qr data refresh ... --lookback-days N` | 新票首次拉 OHLCV 的窗口(默认 730) |
| `qr data freshness [--scope X] [--symbols A,B]` | 每 scope 的过期/缺失报告;Claude 拿 `stale_symbols` 直接喂 refresh |
| `qr screen run --expr "..."` | 基本面筛选:`pe < 30 and peg < 1.5 and sector == 'Technology'` 类 Python 表达式 |
| `qr screen run --technical "..."` | 技术扫描:`above_sma[200],macd_golden_cross[5]` 等命名 predicate,逗号分隔 |
| `qr screen run ... --name X` | 保存 screen 定义(`screens` 表 upsert);所有 run 都进 `screen_runs` |
| `qr screen list` / `qr screen runs [--name X]` | 列出保存的 screen / 历史 run |
| `qr screen diff --from RID1 --to RID2` | 两次 run 的 added/removed/kept 比对 |
| `qr screen fields` | 列出 `--expr` 允许字段 + `--technical` 可用 predicate |
| `qr value SYM [--model X]` | 估值:`X ∈ {dcf, peg, multiples, all}`,持久化到 `valuation_snapshots` |
| `qr value SYM --assumptions '{"growth_rate": 0.10, ...}'` | DCF 假设覆盖(JSON) |
| `qr holdings sync` | 从 IBKR Flex (`FLEX_TOKEN_KEY` + `FLEX_QUERY_ID_LIVE`) 拉持仓快照 |
| `qr holdings import-csv --file PATH [--account A] [--as-of YYYY-MM-DD]` | 从 CSV 导入持仓 |
| `qr holdings list [--account A] [--as-of latest\|YYYY-MM-DD]` | 列当前持仓,默认每 (account, symbol) 取最新 |
| `qr holdings history --symbol SYM [--account A] [--limit N]` | 单票快照历史(新→旧) |
| `qr research bundle SYM [--no-save]` | 一站聚合 profile/financials/ratios/estimates/valuation/holdings/news → 一个 JSON,持久化到 `research_bundles` |
| `qr research news --symbols A,B [--limit N]` | 从 FMP 拉新闻进 `news_items`(夸 plan 402 软失败) |
| `qr research list [--symbol S] [--limit N]` | 列历史 bundle(新→旧) |
| `qr research show BUNDLE_ID` | 看某次 bundle 的完整 payload |
| `qr ledger add SYM --side buy\|sell [--thesis "..." --confidence N --tags A,B]` | 记一笔决策,自动 snapshot 当时的 research_bundle |
| `qr ledger track` | 对所有决策算 1w/1m/3m/6m 远期收益 vs SPY + sector ETF |
| `qr ledger list [--symbol] [--side]` | 列历次决策 |
| `qr ledger scorecard --group-by confidence\|sector\|tag [--horizon 1w\|1m\|3m\|6m]` | 按维度看 avg alpha / return |
| `qr ledger show DECISION_ID` | 单笔决策 + 跟踪表 |
| `qr morningcall [--account A] [--as-of latest\|YYYY-MM-DD] [--save] [--news N]` | 组合晨报:逐持仓精简视图 + 组合层 sector/movers,可选落 `MorningCallSnapshot` |
| `qr earnings SYM [--limit N] [--transcript]` | 财报 actual-vs-est surprise + 论点陈列(纯 warehouse;`--transcript` 在线取,402 软失败) |
| `qr backtest run --symbols A --start D --end D (--strategy NAME\|--strategy-file PATH) [--params k=v,...] [--benchmark SPY] [--raw]` | 回测内置/外部策略,落 `backtest_runs` |
| `qr backtest list [--strategy X] [--limit N]` · `qr backtest show RUN_ID` | 列回测 run / 看单次(指标+净值+成交) |
| `qr signal research --factor F [--horizon 1w\|1m\|3m\|6m] [--quantiles N] [--rebalance monthly\|weekly] [--name X]` | 因子 IC/分位/衰减,落 `signals`/`signal_runs` |
| `qr signal factors` · `qr signal list` · `qr signal runs [--factor F]` · `qr signal show RUN_ID` | 列因子注册表 / 保存信号 / run 历史 / 单次 run |

每个命令在 stdout 输出**正好一个** JSON envelope,exit code 0=ok / 1=error。

## JSON Envelope 契约

所有 CLI 命令统一返回:

```json
{
  "ok": true,
  "schema_version": "1",
  "as_of": "2026-05-20",
  "data_freshness": {"fmp": "live"},
  "snapshot_id": null,
  "code_version": "git:5d5b78d",
  "data": { /* 命令结果 */ },
  "error": null
}
```

失败时 `ok: false`、`data: null`、`error: {code, message, details}`,详见 [`quant_researcher/contract.py`](quant_researcher/contract.py)。

## 项目结构

```
quant_researcher/
├── __init__.py        包版本
├── cli.py             typer 入口 (qr db / qr universe / qr data)
├── config.py          pydantic-settings — 读 .env, 规范化 DSN scheme
├── contract.py        Envelope + ErrorDetail + code_version 探测
├── db.py              SQLAlchemy Base / engine / session_factory
├── universe.py        Watchlist 解析 + replace_universe
├── data/
│   ├── fmp.py         FMP REST client (httpx, token bucket, retry+jitter)
│   └── refresh.py     refresh_profile / refresh_quotes / refresh_financials /
│                      refresh_ratios / refresh_estimates
├── screen/
│   ├── indicators.py  numpy 实现的 SMA/EMA/MACD/RSI/rolling_max/min
│   ├── expression.py  AST-sandbox 的基本面表达式解析器
│   ├── technical.py   命名 predicate DSL (above_sma/macd_cross/rsi/...)
│   └── engine.py      state 加载 + run_screen + diff_runs + 持久化
├── valuation/
│   ├── wacc.py        Bloomberg-adjusted β + CAPM
│   ├── helpers.py     仓库访问 (历史 FCF / 净债 / 股本 / 同业中位数 / 增长 CAGR)
│   ├── dcf.py         DCF-FCFF + Gordon terminal + 5×5 sensitivity
│   ├── peg.py         PEG + Lynch 公平 P/E
│   ├── multiples.py   P/E / EV/EBITDA / P/S 同业中位数 × 公司指标
│   └── engine.py      value_company orchestration + ValuationSnapshot 持久化
├── holdings/
│   ├── ibkr_flex.py   IBKR Flex Statement API client (SendRequest + 轮询 + XML 解析)
│   ├── csv.py         parse_holdings_csv (header 校验 + 类型强转)
│   └── importer.py    flex / csv → Holding 统一上插
├── research/
│   ├── bundler.py     build_bundle (DB → JSON aggregator) + bundle (持久化)
│   ├── morningcall.py build_morning_call (持仓+仓库 → 组合晨报) + save
│   ├── earnings.py    read_earnings (actual-vs-est surprise + 论点陈列)
│   └── refresh.py     refresh_news (FMP /news/stock-latest → news_items dedup)
├── ledger/
│   ├── sectors.py     sector → SPDR ETF 映射 (XLK/XLF/XLE/...)
│   └── engine.py      record_decision + track_decisions + scorecard
├── signals/
│   ├── factors.py     因子注册表 (fundamental 复用 screen FIELDS + price 动量/反转/波动)
│   ├── panel.py       PIT 面板 (仓库 I/O + PriceSeries numpy)
│   └── engine.py      run_signal + IC/分位/衰减 + 持久化
├── backtest/
│   ├── runner.py      run_backtest 唯一入口 (CLI + Python 都走它)
│   ├── loader.py      --strategy-file importlib 加载外部 BaseStrategy
│   └── strategies/    内置策略注册表 (6 个单标的: sma/macd/rsi/bollinger/donchian/buy-hold)
├── engine/            【移植自 quant-engine】core/strategy/portfolio/indicators/
│                      data/execution/risk/analytics/engine.py + warehouse_feed.py
└── models/            SQLAlchemy 声明式 model
    ├── securities.py  symbol master
    ├── universe.py    watchlist 成员
    ├── profile.py     FMP /profile
    ├── prices.py      OHLCV (composite PK)
    ├── financials.py  IncomeStatement / BalanceSheet / CashFlow (共享 mixin)
    ├── ratios.py      FinancialRatios
    ├── estimates.py   AnalystEstimate (forward consensus)
    ├── screens.py     Screen (定义) + ScreenRun (结果快照)
    ├── valuation.py   ValuationSnapshot (一行一模型一估值)
    ├── holdings.py    Holding (PK = account+symbol+as_of_date,每天快照累加)
    ├── research.py    NewsItem (新闻缓存) + ResearchBundle (聚合快照)
    ├── decisions.py   Decision + DecisionTracking (买卖决策 + 远期 alpha 表)
    ├── signals.py     Signal (定义) + SignalRun (IC/分位/衰减 run 快照)
    ├── backtest.py    BacktestRun (回测 run + 指标 + 净值 + 成交)
    └── morningcall.py MorningCallSnapshot (组合晨报快照)
tests/                 pytest, in-memory SQLite + respx mock
docs/                  features.md (D1–D12) + implementation-plan.md (I1–I8 + M0–MH)
config/watchlist.sample.txt   填 ticker,每行一个;# 开头是注释
```

### 研究数据包(MD)

一个命令把仓库里这只票的"全貌"打包成 JSON,Claude skill 直接消费、不用反复查仓库:

```bash
# 先抓点新闻(可选,FMP 某些 plan 走 402 就软失败,bundle 仍然能出)
uv run qr data refresh --scope all --periods annual                  # 把 profile/financials/ratios/estimates 灌好
uv run qr research news --symbols AAPL,MSFT,NVDA --limit 30          # 灌新闻

# 单票深度包
uv run qr research bundle AAPL                                       # 默认 save=True, news_limit=10
#   envelope.data.bundle_id = UUID, .payload = 完整聚合 dict
```

bundle 包含:
- `profile` (sector/industry/exchange/beta/market_cap)
- `latest_price` (最新 OHLCV)
- `ratios_latest_annual` (PE/PEG/EV/EBITDA/ROE 等 14 个)
- `income_statement_recent` / `balance_sheet_recent` / `cash_flow_recent` (最近 5 期)
- `estimates_forward` (未来 4 期一致预期)
- `valuation_snapshots` (每个模型最新一条)
- `holdings` (每账户最新持仓)
- `news` (最近 N 条标题/url/source)
- `transcript_excerpt` (caller 传入,留 hook)

数据缺一项 bundle 不挂,该 section 是 None 或 [];可复现:`bundle_id` + `code_version` 入 `research_bundles`。

### 决策账本(MF)

每个 Claude 买卖决定 + 论点 + 数据快照 + 远期 alpha,持久化可复现:

```bash
# 记一笔
uv run qr ledger add NVDA --side buy --thesis "AI 周期延续, EPV upside" \
  --confidence 4 --tags AI,cycle,growth
#   → bundle_id 自动指向当时的 research_bundles 快照, price_at_open 是当日 close

# (过一阵)算远期收益:1w/1m/3m/6m vs SPY + 行业 ETF
uv run qr ledger track
#   只算"已经过期"的 horizon (e.g. 决策开了 35 天 → 1w + 1m 都算; 3m/6m skip)

# 看 scorecard
uv run qr ledger scorecard --group-by confidence --horizon 1m
#   返回每个 confidence 分组的 decision_count / avg_return_pct / avg_alpha_pct / median_alpha_pct
#   群体按 avg_alpha 降序

uv run qr ledger scorecard --group-by sector --horizon 3m
uv run qr ledger scorecard --group-by tag --horizon 6m

# 看单笔
uv run qr ledger show <decision_id>
```

**Alpha 计算**:`alpha = symbol_return − benchmark_return`,benchmark 优先用 sector ETF(XLK/XLF/XLE/XLV/...),没匹配到就退到 SPY。sector→ETF 映射在 [`quant_researcher/ledger/sectors.py`](quant_researcher/ledger/sectors.py)。

**Short 决策(side=sell)**:return 取反 —— 股价跌 10% 的 short = +10% return,语义跟 long 一致。

**Tracking 容忍 ±3 天**:某 horizon 的 target_date 附近 3 天内必须有 bar,否则该格写 None(避免拿月初的价格冒充月末)。

### 组合晨报(ME)

```bash
# 逐持仓精简视图 + 组合层 sector/movers(不是 N 份完整 bundle)
uv run qr morningcall                                   # 默认全账户、最新持仓、每票 1 条新闻
uv run qr morningcall --account U1234567 --news 2       # 限定账户、每票 2 条
uv run qr morningcall --as-of 2026-05-20 --save         # 历史某日 + 落 MorningCallSnapshot
```

逐持仓:权重 / 盈亏% / 日涨跌(close-to-close) / 精简 ratios / 估值 upside / 1 条新闻 / 关联 decision。组合层:总市值 / 总盈亏 / sector 暴露 / top-bottom movers / 现金。诚实约定:跨币种只 raw sum + note;现金取不到 → None + note。

### 财报速读(D / #6)

```bash
uv run qr earnings AAPL                                 # 最近 4 期 actual vs 一致预期 + 论点陈列
uv run qr earnings AAPL --limit 8 --transcript          # 8 期 + 在线取最新纪要摘录(402 软失败)
```

纯 warehouse 读:把最近 N 期 `income_statement` actual 按共享 PK join `analyst_estimates`,有估值就算 EPS/营收 surprise。**caveat**:估值是 forward + merge 覆盖的 → 历史期 surprise 稀疏,`estimate_available` / `estimates_matched` 把覆盖率摆明,绝不在没估值时暗示 beat/miss。

### 信号研究(MG)

给全 universe 在月度(或周度)rebalance 日按因子排名,量它对**前瞻收益**的预测力(IC / 分位收益 / 衰减):

```bash
uv run qr signal factors                                # 列因子注册表(fundamental 复用 screen 字段 + price 动量/反转/波动)
uv run qr signal research --factor momentum_12_1 --horizon 3m
uv run qr signal research --factor roe --quantiles 5 --rebalance monthly --name "roe_q5"
uv run qr signal runs --factor momentum_12_1            # run 历史
uv run qr signal show <run_id>                          # 单次完整 IC/分位/衰减/coverage
```

**因子**:fundamental(从 `financial_ratios` 取,**PIT 走 `IncomeStatement.known_at`** 防泄漏)+ price(`momentum_12_1` / `momentum_6_1` / `reversal_1m` / `realized_vol_3m`)。**诚实 coverage**:2 年价格 + 每股 ~2 份年报 → 基本面因子准静态、IC 自相关、t-stat 虚高;`coverage.warnings` 在样本薄时明确警告。落 `signals` / `signal_runs` 可复现。

### 回测(MH)

quant-engine 整包移植 + warehouse feed。内置 6 个单标的策略,或 `--strategy-file` 加载外部 `BaseStrategy` 子类:

```bash
# 内置策略: sma_crossover / buy_and_hold / macd_crossover / bollinger_reversion / rsi_reversion / donchian_breakout
uv run qr backtest run --symbols AAPL --start 2023-01-01 --end 2025-01-01 \
  --strategy sma_crossover --params "fast_period=20,slow_period=50"

# 对基准算 alpha/beta(SPY 须在 universe 且已刷 quote)
uv run qr backtest run --symbols AAPL --start 2023-01-01 --end 2025-01-01 \
  --strategy macd_crossover --benchmark SPY

# 外部策略文件(本地执行,不沙箱)
uv run qr backtest run --symbols AAPL --start 2023-01-01 --end 2025-01-01 \
  --strategy-file ./my_strategy.py --strategy-class MyStrategy

uv run qr backtest list                                 # 列 run + headline 指标
uv run qr backtest show <run_id>                        # 完整指标 + 净值曲线 + 成交日志
```

**默认 adjusted**:用 `adj_close/close` factor 回调整根 OHLC(split/dividend 正确),`--raw` 关掉。fee 模型 `zero|per-share|percentage`。落 `backtest_runs`(summary 不含大字段,equity/trades 由 `show` 取)。risk/margin 模块已移植但 CLI v1 不接。

## 路线图

按 D4 顺序 `A → B → C → D/E/F → G → H` 逐域扎实,**v1 八能力域已全部闭环**:

- **M0** 脚手架 ✅ — uv + `qr` + JSON envelope + SQLAlchemy `Base` + `qr db status|init|ping`
- **MA** 仓库 + 数据(域 A)✅
  - **MA-1** ✅ — `universe` / `securities` + `qr universe set/list`
  - **MA-2** ✅ — FMP client + `profiles` / `daily_prices` + `qr data refresh --scope profile|quote`
  - **MA-3** ✅ — 财报三表 + ratios + estimates + `--scope financials|ratios|estimates`
  - **MA-4** ✅ — `qr data freshness` + `qr data refresh` 默认只刷过期 + `--force`
  - **MA-5** ✅ — `refresh_ratios` 接 `/key-metrics` 补全 ROE/ROA/fcf_yield + `roic`/`earnings_yield`
- **MB** 筛选 ✅ — AST-sandbox 表达式 + 9 个技术 predicate + `screens` / `screen_runs` + diff
- **MC** 估值 ✅ — DCF-FCFF + Bloomberg-β WACC + PEG + 行业倍数 + 5×5 sensitivity + `valuation_snapshots`(EPV/DDM 延后)
- **MD** 研究包 ✅ — `news_items` + `research_bundles` + `qr research bundle/news/list/show`;另含 `qr earnings`(财报速读)
- **ME** 持仓 ✅ — IBKR Flex Python client + CSV importer + `holdings` 快照 + `qr holdings sync/import-csv/list/history` + `qr morningcall`(组合晨报)
- **MF** 决策账本 ✅ — `decisions` + `decision_tracking` + 1w/1m/3m/6m vs SPY+sector ETF + scorecard
- **MG** 信号研究 ✅ — `signals` / `signal_runs` + 因子注册表(fundamental + price)+ IC/分位/衰减 + `qr signal research/factors/list/runs/show`
- **MH** 回测 ✅ — quant-engine 整包移植 + `WarehouseDataFeed` + 6 内置策略 + `--strategy-file` + `qr backtest run/list/show`

后续候选(v1 之外):EPV/DDM 估值模型、反向 DCF、多标的回测(需修上游 bar 对齐)、风险/止损模块接入 CLI、MCP 薄适配层。

## 开发

```bash
uv sync                          # 安装运行时 + dev 依赖
uv run ruff check .              # lint
uv run ruff check --fix .        # autofix
uv run pytest -q                 # 测试 (in-memory SQLite + respx,不依赖真实 FMP/DB)
uv run pytest tests/test_refresh.py -k known_at   # 关键 D6 验证子集
```

CI 在 push / PR 自动跑 ruff + pytest(`.github/workflows/ci.yml`)。

### 添加新表 / 改 schema

D11:**不用 Alembic**。新表 → 加 `models/X.py` → 在 `models/__init__.py` re-export → `qr db init` 落地。**改/删既有列需在 Neon console(SQL Editor)手工 ALTER**(`create_all(checkfirst=True)` 只加不改)。

### 添加新 scope / 新 endpoint

模式参考 MA-2/MA-3(`data/fmp.py` 加方法 → `data/refresh.py` 加 `refresh_X` → `cli.py` 扩 `_VALID_SCOPES` + if-block → 4 个测试文件加用例)。MagicMock(spec=FMPClient) 跑业务测试,respx 跑 HTTP 层测试。

## 设计决策

- 关键决策落 [`docs/features.md`](docs/features.md) §7(D1–D11,需求层面)和 [`docs/implementation-plan.md`](docs/implementation-plan.md) §2(I1–I8,实现层面)。
- 不要在代码里直接改设计 —— 先改 docs,记录理由,再落代码。

## License

未发布 license(personal-first 项目,开源但不为通用性牺牲顺手程度)。需要 fork / 复用,提 issue 沟通。
