# Plan: Real-Time News → Valuation → Trading-Signal Service

> Status: **DRAFT for review** — requirements-first, no code yet.
> Author: Claude · Date: 2026-05-29 · Branch: `claude/realtime-news-valuation-signals-GFZuk`
>
> This document records the proposed evolution of `quant-researcher` from a
> one-shot CLI into a **long-running service** that ingests real-time market
> events, reprices the affected company, emits a trading signal via a
> Claude-Agent-SDK "brain", persists it, and notifies the operator — built
> locally, deployable to AWS.

---

## 0. 思路转变（the paradigm shift）

| | 现状 (v1) | 目标 (v2) |
|---|---|---|
| 形态 | 一次性 CLI，`qr <group> <cmd>` 跑完即退 | **常驻服务 (daemon)**，7×24 运行 |
| 触发 | 人在外面用自然语言驱动 Claude 编排 | **实时事件**（评级调整/并购/财报）自动触发 |
| 决策 | 人/Claude 在对话里给判断 | **Claude Agent SDK** 在服务内自动决策 |
| 对外接口 | 无（本地 stdout JSON） | **REST API**（HTTP/HTTPS） + webhook 入口 |
| 输出 | stdout envelope | **交易信号落库 + 邮件/短信通知** |
| 部署 | 本地 shell | 本地开发 → **AWS** 生产 |

**核心设计原则（最重要的一条）：不重写 `qr` 核心。**
现有的 `value_company()` / `research.bundle()` / `record_decision()` / `track_decisions()`
全部是干净的、可调用的纯函数，本来就是为编排而设计的。v2 的做法是把
`qr` 整体**降为"工具/库层"**，在它上面**新加一个服务层**
（`quant_researcher/service/`），服务层调用这些函数。
现有的 CLI、JSON envelope 契约、各 domain rules **全部保留不动**。

---

## 1. 目标架构（target architecture）

```
                          ┌─────────────────────────────────────────────┐
   实时消息源              │              qr-service (常驻进程)            │
 (付费/免费 webhook)       │                                             │
   ┌──────────┐  POST     │  ┌────────────┐    ┌──────────────────────┐ │
   │ Benzinga │ ────────▶ │  │ Ingestion  │    │   Agent Core         │ │
   │ /FMP/... │  webhook  │  │ (FastAPI   │───▶│  (Claude Agent SDK)  │ │
   └──────────┘           │  │  /webhook) │    │                      │ │
                          │  └────────────┘    │  system prompt:      │ │
   ┌──────────┐  poll     │  ┌────────────┐    │  "你是事件驱动的      │ │
   │ FMP       │ ◀──────── │  │ Poller     │───▶│   定价分析师"         │ │
   │ analyst/  │  fallback │  │ (interval) │    │                      │ │
   │ news API  │           │  └────────────┘    │  tools (见 §3.3):    │ │
   └──────────┘           │         │           │  - classify_event    │ │
                          │         ▼           │  - refresh_data      │ │
                          │  ┌────────────┐     │  - value_company     │ │
                          │  │ Event Queue│     │  - research_bundle   │ │
                          │  │ + dedup    │     │  - compute_signal    │ │
                          │  └────────────┘     │  - record_decision   │ │
                          │                     │  - notify            │ │
                          │                     └──────────┬───────────┘ │
                          │  ┌──────────────────────────────▼──────────┐ │
                          │  │  Persistence (Neon serverless PG)        │ │
                          │  │  events · signals · decisions · snapshots│ │
                          │  └────────┬─────────────────────┬──────────┘ │
                          │  ┌────────▼────────┐  ┌──────────▼─────────┐ │
                          │  │ Signal Monitor  │  │  REST API (查询/控制)│ │
                          │  │ 每日扫盘/目标管理 │  │  GET /signals 等     │ │
                          │  │ 到目标价/跌破止损/ │  └────────────────────┘ │
                          │  │ 持有期到期 → 触发 │                          │
                          │  └────────┬────────┘                          │
                          │  ┌────────▼────────┐                          │
   操作者手机/邮箱 ◀────────│  │ Notifier        │                          │
   (邮件/短信/IM)          │  │ email/SMS/IM    │                          │
                          │  └─────────────────┘                          │
                          └─────────────────────────────────────────────┘
```

> 注意系统里有**两条相对独立的回路**：
> **(A) 事件回路**（上半，实时事件 → 生成信号）和
> **(B) 监控回路**（下半，`Signal Monitor` 每日/盘中扫描已落盘的 open 信号，
> 到目标价 / 跌破止损 / 持有期到期就触发通知与状态流转）。
> 信号**不是发完就完**——它是一个有生命周期、被管理的"头寸"。

### 端到端事件流（一条消息从进来到落地）

1. **接收**：实时源把一条消息 POST 到 `/webhook/<source>`（或 poller 主动拉到一条新事件）。
2. **入队 + 去重**：写入 `raw_events` 表，按 `(source, external_id)` 去重，立即返回 `200`（webhook 必须秒回，重活异步做）。
3. **分类**：Agent 调 `classify_event` —— 这是评级调整？并购？财报超预期？还是宏观噪声（直接丢弃）？提取出 `symbol`、`event_type`、`direction`、关键数字（新目标价/收购价等）。
4. **取数**：Agent 调 `refresh_data --symbols X`（只刷该票的 quote/ratios/estimates），保证估值输入新鲜。
5. **重定价**：Agent 调 `value_company`。**关键：单纯重跑不会变**，因为 DCF 输入是季报数据。所以 Agent 要**读懂新闻、调整假设**（如：被收购→以收购价为锚；评级下调→下调增长率假设），再跑 `value`/`research_bundle` 拿到新的公允价值区间。
6. **生成信号**：Agent 调 `compute_signal`——这是**交易体系**(System B，见 §1.5)在干活。它把估值体系(System A)的公允价值当作**输入之一**，再结合现价偏离、事件方向、置信度，产出一条完整的量化信号：`{direction, conviction, entry_price, target_price, stop_loss, horizon, suggested_size}`。
7. **落库**：调 `record_decision`（已自动 snapshot 当时数据，可回放）+ 写 `signals` 表（含 target/stop/horizon/status=open）。
8. **通知**：调 `notify` 把信号摘要发到邮箱/手机。
9. 操作者在自己的模拟盘**手动**下单（人始终在环里）。
10. **此后进入监控回路**：`Signal Monitor` 每天扫这条 open 信号，直到 target_hit / stopped_out / expired，再次通知你处置。

---

## 1.5 两套体系：估值体系 vs 交易体系（核心解耦）

这是 v2 最重要的一个概念区分。系统里有**两套互相独立的体系**，职责完全不同：

| | **System A — 估值体系**（已有） | **System B — 交易体系**（新建） |
|---|---|---|
| 别名 | 基于财报的估值体系 | 价格体系 / 交易信号体系 |
| 回答的问题 | "这家公司**值多少钱**？" | "**现在该买还是卖**？目标、止损、持有多久？" |
| 输入 | 财报：现金流、增长、利润率… | 现价、估值偏离、事件、（未来）技术面/量价 |
| 节奏 | 慢（季度财报驱动） | 快（事件/价格驱动） |
| 现有实现 | `valuation/`：DCF / PEG / multiples | **无，需新建** `quant_researcher/signal_system/` |
| 输出 | 公允价值区间 | 一条结构化**交易信号**（见下） |

**两者的关系：单向、松耦合。** System A 的公允价值只是 System B 的**输入之一**，
而不是唯一来源——这样将来 System B 可以**完全不依赖 DCF**，纯靠价格/事件/算法跑。
绝不能把交易逻辑塞进 `valuation/`，那会把两套节奏完全不同的东西焊死。

### 交易信号的结构（模仿量化框架）

参考成熟量化平台的 signal 机制，一条信号必须包含这些要素：

| 字段 | 含义 |
|---|---|
| `direction` | 买 / 卖 / 持有 |
| `conviction` | 信号强度 / 置信度 |
| `entry_price` | 入场参考价（= 信号生成时现价） |
| `target_price` | **目标价**（如"现价 1600 → 目标 2000"） |
| `stop_loss` | **止损价** |
| `horizon` | **持有时间**（如 1 个月、3 个月） |
| `suggested_size` | 建议仓位，用 **risk-based**：`size = 权益 × 风险% / (entry − stop_loss)`，起步风险% = 1%，conviction 当乘数(0.5%–2%)。把 stop_loss 直接用起来，每笔风险固定。 |
| `generated_by` | `llm` 或 `algo`（见下，可插拔） |
| `status` | `open / target_hit / stopped_out / expired / closed` |

### 信号生成器可插拔（现在 LLM，将来算法）

`SignalGenerator` 做成一个接口，两种实现可热插拔：
- **`LlmSignalGenerator`**（Phase 1）：Claude Agent SDK 读事件 + 估值 + 上下文，用语言推理生成信号。**起步快**。
- **`AlgoSignalGenerator`**（将来）：在积累大量历史数据后，用算法/模型（规则阈值或 ML）生成信号。**可回测、可量化**。

两种生成器写进同一张 `signals` 表、走同一条监控回路，靠 `generated_by` 区分，
方便用 `qr backtest` / `ledger scorecard` 对比"LLM 信号 vs 算法信号"谁的 alpha 更好。

### 信号生命周期管理（Signal Monitor）

落盘 ≠ 结束。新增 `Signal Monitor` 组件，定时（每日收盘后 + 可选盘中）扫描所有 `open` 信号：

- 现价 **≥ target_price** → 标 `target_hit`，通知"目标达成，考虑止盈"。
- 现价 **≤ stop_loss** → 标 `stopped_out`，**第一时间**通知"跌破止损"。
- now **≥ created_at + horizon** → 标 `expired`，通知"持有期到，复盘是否到达目标"。

这条回路天然接上现有的 `ledger track`（前向收益）/ `scorecard`（按 conviction/来源打分），
形成"生成信号 → 管理 → 复盘打分 → 反哺生成器阈值"的闭环。

---

## 2. 组件设计

### 3.1 Ingestion（实时接入层）
- **FastAPI** 应用，`POST /webhook/{source}`：校验签名 → 写 `raw_events` → 入队 → 立即 `200`。
- **Poller**（主力源 = FMP）：`asyncio` 定时任务，按 watchlist 轮询 FMP 的
  `analyst/grades`（**评级调整**，字段 `date/gradingCompany/previousGrade/newGrade/action`，
  实测 NVDA 2 天新鲜）、`analyst/price-target-summary`、`news/stock` 端点。
  **grades 端点返回全量历史**，所以 poller 要**与上次见过的最新一条 diff** 才算新事件。
- **能力探测（capability probe）**：服务启动时用真实 key 实打 grades / price-target /
  stock-news / quote，记录"本 key 能用哪些端点"；运行时任一端点 **402 → 软降级**
  （沿用现有 `qr` 行为），缺评级就退用新闻+财报，绝不闷崩。
  > ⚠️ FMP Starter（第二档）**未必包含 analyst grades / price-target**（历来 Premium+）。
  > 待用户用自己的 key 验证；无论结论如何，probe + 402 降级都让系统优雅适配。
- 抽象一个 `EventSource` 接口，付费 webhook 源与免费 poll 源产出**统一的内部事件结构**，
  下游不关心来源。
- **去重**是头等大事：同一事件多源/重推必须只触发一次。

### 3.2 Agent Core（Claude Agent SDK —— 决策大脑）
- 用 **Claude Agent SDK**（Python）跑一个 agent loop：system prompt 定义角色
  （"事件驱动的定价分析师，目标是产出一条可执行交易信号"），工具集见 §3.3。
- 每条事件 = 一次 agent 会话。Agent 自主决定调哪些工具、调几次、要不要放弃（噪声事件直接 no-op）。
- 这一段正好**契合本项目"工具出数据、Claude 出判断"的哲学**——只是把判断从人机对话搬进了服务进程。
- 成本控制：噪声事件应在 `classify_event` 后尽早短路，避免每条新闻都跑全套估值烧 token。

### 3.3 Tool 层（复用 qr，零重写）
把现有函数包成 agent 可调用的工具（in-process function tools，或经 MCP 暴露）：

| 工具 | 背后函数（已存在） |
|---|---|
| `refresh_data` | `data/refresh.py::refresh_*` |
| `research_bundle` | `research/bundler.py::bundle()` |
| `value_company` | `valuation/engine.py::value_company()` |
| `record_decision` | `ledger/engine.py::record_decision()` |
| `classify_event` | **新增**（轻量分类/抽取） |
| `compute_signal` | **新增** = 交易体系 System B（§1.5），产出含 target/stop/horizon 的完整信号 |
| `notify` | **新增**（通知层） |

`compute_signal` 背后是 `quant_researcher/signal_system/`（System B），与 `valuation/`（System A）**完全隔离**。

### 3.4 信号 schema + 持久化
- 新表 `raw_events`（原始消息+去重键）、`signals`（结构化交易信号）。
- `signals` 字段（对齐 §1.5 的量化信号要素）：
  `id, event_id, symbol, direction(buy/sell/hold), conviction, entry_price,
  target_price, stop_loss, horizon_days, suggested_size,
  fair_value_base（来自 System A，可空）, deviation_pct, thesis,
  generated_by(llm/algo), snapshot_id, created_at, expires_at, status`。
- **DB 定 Neon（serverless Postgres）**，开发+生产统一，省一次 SQLite→PG 的迁移。Neon scale-to-zero、按用量计费，且是标准 PG wire protocol，现有 SQLAlchemy/psycopg 直接连。Schema 走现有 `db.py` 的 Base + Alembic/SQL 迁移。注意 serverless DB 可能 cold-start（首查有几百 ms 延迟）+ 连接走 pooler（`?sslmode=require`、用 pooled endpoint）。
- 注意现有测试用内存 SQLite——`DateTime(timezone=True)` 列在测试里仍要 `_naive_utc` 归一。

### 3.5 Signal Monitor（监控回路，全新）
- **分层扫盘频率（已定）**：
  - **收盘后每日 1 次** → 查 `target_hit` / `horizon` 到期（Phase 2 先只做这层）。
  - **盘中每 30 分钟** → 只查 `stop_loss` 跌破（Phase 3 加）。
  - 不做 tick/1分钟级（过度设计）。
- **省配额关键**：用 FMP `batch-quote`，一次调用拿一批 symbol 报价，扫盘成本与 watchlist 大小无关。
- 取最新 quote → 命中 `target_price` / `stop_loss` / `expires_at` 任一 → 流转 status + 触发 `notify`。
- 跌破止损是**最高优先级**，盘中 30 分钟那层就是为它。
- 复用现有 `ledger track` 给每条信号算前向收益，喂回 `scorecard` 打分。

### 3.6 通知层（全新，最容易）
- 抽象 `Notifier` 接口，先实现 **邮件 (SMTP)**（几十行），后续可加短信（Twilio）或 IM（Telegram/Server酱，国内手机更现实）。
- 配置走现有 `config.py`（pydantic-settings，从 env 读）。

### 3.7 REST API（对外接口）
- `POST /webhook/{source}` — 实时入口
- `GET /signals?symbol=&status=&since=` — 查信号（含生命周期状态）
- `GET /events/{id}` — 查单条事件全链路（含 snapshot，可回放）
- `POST /events/replay` — 手动喂一条事件（开发/测试用，也是 Phase 1 的验证手段）
- `GET /healthz` — 健康检查（任何容器编排/负载均衡都需要）

---

## 3. 与现有架构契约的兼容

- **JSON envelope 契约不变**：CLI 仍是 CLI。服务层**直接调用底层函数**，不去 subprocess 跑 `qr`（避免一命令一 envelope 的限制反而碍事）。
- **新代码隔离在两个新包**：`quant_researcher/service/`（FastAPI/agent/monitor/notify）和 `quant_researcher/signal_system/`（System B，§1.5）。不动现有 domain。
- 复用现有 gotchas 经验：lazy-import、`_emit` 不放 try、SQLite 无 tz 用 `_naive_utc`。
- 新增依赖：`fastapi`、`uvicorn`、`claude-agent-sdk`（或 `anthropic`）、`anyio`。放进 `pyproject.toml` 主依赖；通知/部署相关放 optional extras。

---

## 4. 技术栈选型

| 关注点 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | async、webhook 友好、自带 OpenAPI |
| Agent | Claude Agent SDK (Python) | 你点名要的决策大脑 |
| 实时数据 | FMP `analyst`/`news`（起步）→ 付费 webhook 源（升级） | 评级调整正是 FMP analyst 端点；起步成本低 |
| 数据库 | **Neon（serverless Postgres）**，开发+生产统一 | 已拍板；scale-to-zero 按用量计费，标准 PG，复用现有 SQLAlchemy |
| 通知 | SMTP 起步 → Telegram/Twilio | 渐进 |
| 部署 | **标准 Docker 容器**，平台无关（见 §4.5） | 先跑 AWS，能随时搬走 |

> 注：本对话环境里已经挂了 FMP、Gmail 等 MCP server——
> 它们正好可以在**开发/验证阶段**当现成工具用（FMP 取评级/新闻、
> Gmail 发通知），生产再换成进程内实现。Neon 走标准 PG 连接，不依赖 MCP。

## 4.5 部署哲学：平台无关（platform-agnostic）

**硬性约束：代码绝不绑定任何云的专有 API。** 整个服务打成一个标准 Docker 容器，
对外部资源的依赖全部走可替换的抽象：

- **存储** → 只认 Postgres connection string（Neon / RDS / 自建 PG 都行）。
- **密钥** → 只认环境变量（本地 `.env` / AWS Secrets Manager / 任何 secret 注入都行）。
- **通知** → `Notifier` 接口（SMTP / Twilio / Telegram 可换）。
- **数据源** → `EventSource` 接口（FMP / 付费 webhook 可换）。

这样在哪跑都行：本地 `docker compose`、AWS、GCP Cloud Run、Fly.io、一台 VPS。

**关于 AWS / serverless 的张力（你提到的点）：**
- 优势：你熟、首次部署启动成本低。
- 矛盾：纯 **serverless（Lambda / Cloud Run scale-to-zero）跑不了"常驻后台"** ——
  我们的 Poller 和 Signal Monitor 需要长期/定时运行。
- **解法**：把职责拆成两类，但都用同一个容器镜像、同一套代码：
  1. **webhook 入口** 是无状态、事件驱动的 → 可以上 serverless（便宜、自动伸缩）。
  2. **Poller + Signal Monitor** 是常驻/定时的 → 要么放一个 **always-on 容器**（ECS Fargate 常驻 / 一台小 EC2），要么用**外部调度器定时唤醒**（EventBridge cron → 跑一次扫盘任务）。
- 因为平台无关，**起步最省事**：一个 always-on 容器把全部职责跑起来；等量大了再按上面拆分。AWS 只是"第一个落脚点"，不是绑定。

---

## 5. 分阶段路线图（建议一里程碑一 PR）

- **Phase 0 — 服务骨架**：建 `service/` + `signal_system/` 包、FastAPI app、`/healthz`、把 `value_company` 等包成 tool、连 Neon、本地 `docker compose` 跑起来。
- **Phase 1 — 闭环 MVP（同步、单源、手动喂事件）**：`POST /events/replay` 喂一条"评级下调"假事件 → Agent 分类→取数→重定价→`LlmSignalGenerator` 产出含 target/stop/horizon 的信号→落库→**发邮件**。**先把整条链跑通**，不接真实时源。
- **Phase 2 — 监控回路**：上 `Signal Monitor`，每日扫 open 信号，到目标价/跌破止损/到期 → 通知 + 状态流转。信号生命周期闭环。
- **Phase 3 — 真实时接入**：接 FMP poller（免费起步）/ 真 webhook 源、事件队列、去重、重试、错误隔离。
- **Phase 4 — 上 AWS（平台无关镜像）**：先一个 always-on 容器跑全部；密钥走 Secrets Manager。
- **Phase 5 — 算法信号 + 质量闭环**：积累数据后做 `AlgoSignalGenerator`，用 `qr backtest` + `ledger scorecard` 对比 LLM vs 算法信号的 alpha，反哺阈值。

---

## 6. 已拍板 / 待拍板

**已拍板：**
- ✅ 数据库 = **Neon（serverless Postgres）**，开发+生产统一。
- ✅ 部署 = **平台无关 Docker 容器**，AWS 作为第一落脚点（起步用单个 always-on 容器）。
- ✅ 两套体系解耦：估值体系(A) 与 交易信号体系(B) 独立；信号含 target/stop/horizon/direction。
- ✅ 信号生成器可插拔：现在 `LlmSignalGenerator`，将来 `AlgoSignalGenerator`。
- ✅ **数据源 = FMP（Starter 第二档）**，poll 模式 + 启动能力探测 + 402 软降级。
  `analyst/grades` 是评级调整主源（待用户验证 Starter 是否含此端点）。
- ✅ **Agent 自主度 = 自动落库 + 自动通知，下单仍人工**（无人工审批环节）。
- ✅ **仓位 = risk-based**，起步每笔风险 1%，`size = 权益×1% / (entry − stop)`，conviction 后续叠乘数。
- ✅ **扫盘频率 = 收盘后每日 + 盘中每 30 分钟查止损**（用 batch-quote 省配额）。

**待用户验证（不阻塞开发）：**
- ❓ 用 Starter key 实打 `grades` / `news/stock`，确认第二档是否含评级+新闻端点；结论只影响数据源降级策略，不影响架构。

---

## 7. 主要风险

- **数据源成本/延迟**：真正秒级的 sell-side 评级源很贵；免费源只能做到准实时。
- **Alpha 衰减**：快新闻几分钟内被市场吃掉，"邮件→人手动下单"的链路只适合**中速的叙事/估值重定价信号**，不适合抢秒级。这点决定了系统定位。
- **LLM 成本**：每条事件一次 agent 会话，噪声不短路会烧钱——`classify_event` 早筛是关键。
- **纯代码重定价的幻觉**：DCF 输入是季报数据，必须靠 Agent 读新闻**改假设**才会真正"重定价"，否则信号是死的。
- **架构漂移风险**：服务层不能侵蚀现有 CLI 契约；严格隔离在 `service/`。
