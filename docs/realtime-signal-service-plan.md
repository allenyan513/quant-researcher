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
                          │  │  Persistence (Postgres / Supabase)       │ │
                          │  │  events · signals · decisions · snapshots│ │
                          │  └──────────────────────────────┬──────────┘ │
                          │  ┌────────────┐  ┌───────────────▼─────────┐ │
   操作者手机/邮箱 ◀────────│  │ Notifier   │  │  REST API (查询/控制)    │ │
   (邮件/短信/IM)          │  │ email/SMS  │  │  GET /signals 等         │ │
                          │  └────────────┘  └─────────────────────────┘ │
                          └─────────────────────────────────────────────┘
```

### 端到端事件流（一条消息从进来到落地）

1. **接收**：实时源把一条消息 POST 到 `/webhook/<source>`（或 poller 主动拉到一条新事件）。
2. **入队 + 去重**：写入 `raw_events` 表，按 `(source, external_id)` 去重，立即返回 `200`（webhook 必须秒回，重活异步做）。
3. **分类**：Agent 调 `classify_event` —— 这是评级调整？并购？财报超预期？还是宏观噪声（直接丢弃）？提取出 `symbol`、`event_type`、`direction`、关键数字（新目标价/收购价等）。
4. **取数**：Agent 调 `refresh_data --symbols X`（只刷该票的 quote/ratios/estimates），保证估值输入新鲜。
5. **重定价**：Agent 调 `value_company`。**关键：单纯重跑不会变**，因为 DCF 输入是季报数据。所以 Agent 要**读懂新闻、调整假设**（如：被收购→以收购价为锚；评级下调→下调增长率假设），再跑 `value`/`research_bundle` 拿到新的公允价值区间。
6. **生成信号**：Agent 调 `compute_signal`——结合"公允价值 vs 现价偏离%"+ 事件方向 + 置信度，产出 `{side, conviction, target_price, suggested_size}`。
7. **落库**：调 `record_decision`（已自动 snapshot 当时数据，可回放）+ 写 `signals` 表。
8. **通知**：调 `notify` 把信号摘要发到邮箱/手机。
9. 操作者在自己的模拟盘**手动**下单（人始终在环里）。

---

## 2. 组件设计

### 3.1 Ingestion（实时接入层）
- **FastAPI** 应用，`POST /webhook/{source}`：校验签名 → 写 `raw_events` → 入队 → 立即 `200`。
- **Poller**（兜底/免费源）：`asyncio` 定时任务，按 watchlist 轮询 FMP 的
  `analyst`（评级/目标价调整）和 `news` 端点，diff 出新事件后走同一条队列。
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
| `compute_signal` | **新增**（偏离% + 方向 → side/size） |
| `notify` | **新增**（通知层） |

### 3.4 信号 schema + 持久化
- 新表 `raw_events`（原始消息+去重键）、`signals`（结构化信号）。
- `signals` 字段建议：`id, event_id, symbol, side, conviction, fair_value_low/base/high,
  price_at_signal, deviation_pct, suggested_size, thesis, snapshot_id, created_at, status`。
- DB：本地开发可继续 SQLite/本地 Postgres；生产用 Postgres（**Supabase** 是一个现成的托管选项，也方便后面做查询面板）。Schema 迁移走现有 `db.py` 的 Base。

### 3.5 通知层（全新，最容易）
- 抽象 `Notifier` 接口，先实现 **邮件 (SMTP)**（几十行），后续可加短信（Twilio）或 IM（Telegram/Server酱，国内手机更现实）。
- 配置走现有 `config.py`（pydantic-settings，从 env 读）。

### 3.6 REST API（对外接口）
- `POST /webhook/{source}` — 实时入口
- `GET /signals?symbol=&since=` — 查信号
- `GET /events/{id}` — 查单条事件全链路（含 snapshot，可回放）
- `POST /events/replay` — 手动喂一条事件（开发/测试用，也是 Phase 1 的验证手段）
- `GET /healthz` — 健康检查（AWS 负载均衡需要）

---

## 3. 与现有架构契约的兼容

- **JSON envelope 契约不变**：CLI 仍是 CLI。服务层**直接调用底层函数**，不去 subprocess 跑 `qr`（避免一命令一 envelope 的限制反而碍事）。
- **新代码隔离在 `quant_researcher/service/`**，不动现有 domain。
- 复用现有 gotchas 经验：lazy-import、`_emit` 不放 try、SQLite 无 tz 用 `_naive_utc`。
- 新增依赖：`fastapi`、`uvicorn`、`claude-agent-sdk`（或 `anthropic`）、`anyio`。放进 `pyproject.toml` 主依赖；通知/部署相关放 optional extras。

---

## 4. 技术栈选型

| 关注点 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | async、webhook 友好、自带 OpenAPI |
| Agent | Claude Agent SDK (Python) | 你点名要的决策大脑 |
| 实时数据 | FMP `analyst`/`news`（起步）→ 付费 webhook 源（升级） | 评级调整正是 FMP analyst 端点；起步成本低 |
| 数据库 | 本地 Postgres → 生产 Supabase/RDS | 复用现有 SQLAlchemy |
| 通知 | SMTP 起步 → Telegram/Twilio | 渐进 |
| 部署 | 本地 Docker → **AWS ECS Fargate**（常驻 + ALB + 健康检查） | 常驻服务最稳的形态；webhook 入口也可单独走 Lambda+API GW |

> 注：本对话环境里已经挂了 FMP、Supabase、Gmail、Vercel 等 MCP server——
> 它们正好可以在**开发/验证阶段**当现成工具用（FMP 取评级/新闻、Supabase 建表、
> Gmail 发通知），生产再换成进程内实现。

---

## 5. 分阶段路线图（建议一里程碑一 PR）

- **Phase 0 — 服务骨架**：建 `service/` 包、FastAPI app、`/healthz`、把 `value_company` 等包成 tool、本地 `uvicorn` 跑起来。把 `qr` 确认为可被库调用。
- **Phase 1 — 闭环 MVP（同步、单源、手动喂事件）**：`POST /events/replay` 喂一条"评级下调"假事件 → Agent 分类→取数→重定价→生成信号→落库→**发邮件**。**先把整条链跑通**，不接真实时源。
- **Phase 2 — 真实时接入**：接 FMP poller（免费起步）/ 真 webhook 源、事件队列、去重、重试、错误隔离。
- **Phase 3 — 上 AWS**：Docker 化、ECS Fargate + ALB、Postgres(RDS/Supabase)、密钥走 Secrets Manager。
- **Phase 4 — 信号质量闭环**：用现有 `qr backtest` + `qr ledger track/scorecard` 回测/打分这些自动信号到底有没有 alpha，反哺 `compute_signal` 的阈值。

---

## 6. 需要你拍板的开放问题

1. **实时数据源**：起步先用现有 FMP（准实时、便宜），还是直接上付费 webhook 源（秒级、贵）？
2. **Agent 自主度**：信号是**自动落库+通知**（人只在模拟盘手动下单），还是**落库前要你点头**？（我建议 Phase 1 全自动落库，但只通知不下单——人始终在最后一环。）
3. **仓位大小（suggested_size）**：按什么规则算？固定额度 / 按 conviction 比例 / 按偏离% ？需要一套 position-sizing 规则。
4. **数据库**：生产用 Supabase（托管、自带面板）还是 AWS RDS（和 ECS 同生态）？
5. **AWS 形态**：常驻 ECS Fargate（推荐），还是 webhook 走 Lambda + 估值走容器的混合？

---

## 7. 主要风险

- **数据源成本/延迟**：真正秒级的 sell-side 评级源很贵；免费源只能做到准实时。
- **Alpha 衰减**：快新闻几分钟内被市场吃掉，"邮件→人手动下单"的链路只适合**中速的叙事/估值重定价信号**，不适合抢秒级。这点决定了系统定位。
- **LLM 成本**：每条事件一次 agent 会话，噪声不短路会烧钱——`classify_event` 早筛是关键。
- **纯代码重定价的幻觉**：DCF 输入是季报数据，必须靠 Agent 读新闻**改假设**才会真正"重定价"，否则信号是死的。
- **架构漂移风险**：服务层不能侵蚀现有 CLI 契约；严格隔离在 `service/`。
