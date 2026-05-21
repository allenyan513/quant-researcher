# quant-researcher

> Claude Code 编排的美股投研底座 —— 提供"取数 + 计算 + 持久化 + 可复现",叙述交给 Claude。

[![CI](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml/badge.svg)](https://github.com/allenyan513/quant-researcher/actions/workflows/ci.yml)

## 状态

**v1 alpha,M0 + MA-1/2/3/4 已落地(2026-05-20)。** 目前可用能力:数据库脚手架、Watchlist 管理、FMP 客户端、profile / OHLCV / 三大报表 / 比率 / 分析师一致预期的刷新、freshness 报告、只刷过期数据。下一步 MB(筛选 —— 衍生条件 + 技术扫描)。完整路线图见 [`docs/implementation-plan.md`](docs/implementation-plan.md) §7。

## 这是什么

我有一套围绕美股的个人投研工作流,但散在 `quant-engine`(回测)、`valuescope`(估值 SaaS)、若干 Claude skills(盘前/盘后/深度调研/周报)、FMP MCP 之间。Claude Code 没法把"一句自然语言"顺畅地拆成跨能力的研究链路。

quant-researcher 就是这个串接层 —— 给 Claude Code 用的辅助投研底座:

- **CLI 是唯一接口**(`qr ...`),stdout 输出**稳定 JSON 信封**。Claude Code 经 Bash 调度,在 envelope 间自由组合。
- **自有数据仓库**(Supabase Postgres),从 FMP 拉数据落地,支持 D6 point-in-time(财报 `known_at` 用 FMP `acceptedDate`)。
- **可复现**:每条结果都带 `as_of` / `data_freshness` / `code_version` / 可选 `snapshot_id`。
- **personal-first**:为我自己的 IBKR + Notion + Claude skills 工作流定型。开源但不为通用性牺牲顺手程度。

详细需求见 [`docs/features.md`](docs/features.md)(D1–D11 决策记录);实现蓝图见 [`docs/implementation-plan.md`](docs/implementation-plan.md)(I1–I8 + 里程碑 M0→MH)。

## 快速开始

需要:Python 3.13+、[uv](https://docs.astral.sh/uv/)、Supabase Postgres 项目(或任意 Postgres DSN)、[FMP](https://financialmodelingprep.com) API key。

```bash
git clone git@github.com:allenyan513/quant-researcher.git
cd quant-researcher
uv sync                                                 # 安装依赖到 .venv

cp .env.example .env                                    # 填 QR_DATABASE_URL + FMP_API_KEY
$EDITOR .env

uv run qr db ping                                       # 验证连接
uv run qr db init                                       # 应用 schema (9 张表)
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

## 命令速查

| 命令 | 作用 |
|---|---|
| `qr db ping` | `SELECT 1`,延迟 + Supabase 防 idle pause 用 |
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
└── models/            SQLAlchemy 声明式 model
    ├── securities.py  symbol master
    ├── universe.py    watchlist 成员
    ├── profile.py     FMP /profile
    ├── prices.py      OHLCV (composite PK)
    ├── financials.py  IncomeStatement / BalanceSheet / CashFlow (共享 mixin)
    ├── ratios.py      FinancialRatios
    └── estimates.py   AnalystEstimate (forward consensus)
tests/                 pytest, in-memory SQLite + respx mock
docs/                  features.md (D1–D11) + implementation-plan.md (I1–I8 + M0–MH)
config/watchlist.sample.txt   填 ticker,每行一个;# 开头是注释
```

## 路线图

按 D4 顺序 `A → B → C → D/E/F → G → H` 逐域扎实:

- **M0** 脚手架 ✅ — uv + `qr` + JSON envelope + SQLAlchemy `Base` + `qr db status|init|ping`
- **MA** 仓库 + 数据 (域 A) ✅
  - **MA-1** ✅ — `universe` / `securities` + `qr universe set/list`
  - **MA-2** ✅ — FMP client + `profiles` / `daily_prices` + `qr data refresh --scope profile|quote`
  - **MA-3** ✅ — 财报三表 + ratios + estimates + `--scope financials|ratios|estimates`
  - **MA-4** ✅ — `qr data freshness` + `qr data refresh` 默认只刷过期 + `--force`
- **MB** 筛选(衍生条件 + 技术扫描)— **下一里程碑**
- **MC** 估值(DCF-FCFF / PEG / 倍数 / EPV / DDM)
- **MD/ME/MF** 深度研究包 / 持仓 + morning call / 决策账本
- **MG** 信号研究(因子 IC / 分位 / 衰减)
- **MH** 回测(移植 `quant-engine`)

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

D11:**不用 Alembic**。新表 → 加 `models/X.py` → 在 `models/__init__.py` re-export → `qr db init` 落地。**改/删既有列需在 Supabase dashboard 手工 ALTER**(`create_all(checkfirst=True)` 只加不改)。

### 添加新 scope / 新 endpoint

模式参考 MA-2/MA-3(`data/fmp.py` 加方法 → `data/refresh.py` 加 `refresh_X` → `cli.py` 扩 `_VALID_SCOPES` + if-block → 4 个测试文件加用例)。MagicMock(spec=FMPClient) 跑业务测试,respx 跑 HTTP 层测试。

## 设计决策

- 关键决策落 [`docs/features.md`](docs/features.md) §7(D1–D11,需求层面)和 [`docs/implementation-plan.md`](docs/implementation-plan.md) §2(I1–I8,实现层面)。
- 不要在代码里直接改设计 —— 先改 docs,记录理由,再落代码。

## License

未发布 license(personal-first 项目,开源但不为通用性牺牲顺手程度)。需要 fork / 复用,提 issue 沟通。
