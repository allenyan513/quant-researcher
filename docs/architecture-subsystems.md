# Architecture: 子系统分解（Modular Monolith 基线）

> 本文是实时交易信号服务的**架构基线**——定义子系统边界、命名规范、子系统间消息契约。
> 配套文档：`docs/realtime-signal-service-plan.md`（需求/决策）。跟踪：Issue #53。
>
> 后续所有开发以本文的边界划分为准。

## 0. 核心原则

1. **Modular Monolith（模块化单体），不提前做分布式。**
   一个库 / 一个进程，但子系统之间只通过**清晰的接口和消息契约**通信，不互相摸内部实现。
   等某个子系统真成瓶颈，再把它拎成独立服务——代价极小。
   **边界画在契约上，不画在部署上。**

2. **业界标准 alpha pipeline**：`数据 → 信号/alpha → 组合/风控 → 执行 → 监控归因`。
   本系统是它的事件驱动变体。

3. **命名消歧（最重要）**：两个东西都曾被叫"信号"，必须严格区分：

   | 角色 | 含义 | 术语 |
   |---|---|---|
   | 输入 | 评级调整 / 并购 / insider 买卖 / 重大盈亏——**触发因素** | **Event（催化剂 / Catalyst）** |
   | 输出 | 方向 / 目标价 / 止损 / 持有时间——**分析后的决策** | **Trading Signal（交易信号）** |

   **Event 进来，Trading Signal 出去。** 全代码库统一用这两个词。

## 1. 数据流总览

```
 外部源              ┌──────────────┐                    ┌──────────────┐
 (FMP grades/        │ S1 采集       │   Event            │ S3 信号分析   │
  news/insider,  ──▶ │ Ingestion    │ ─────────────────▶ │ (Agent 大脑) │
  webhook, M&A)      │ 归一化+去重   │                    │ 分类→重定价   │
                     └──────┬───────┘                    │ →生成        │
                            │ write Event                └──┬────────┬──┘
                            ▼                       read facts│        │ Trading Signal
                     ┌─────────────────────────────────────▼─┐      │
                     │ S2 数据层 Data Layer (Neon)            │◀─────┘ write signal
                     │ ① facts事实仓库 ② events ③ signals/    │
                     │ decisions ④ fills/positions           │
                     └──────▲────────────────────┬───────────┘
                  read facts│         read signal│
                     ┌──────┴──────┐      ┌───────▼────────┐    fills/PnL
                     │ S4 估值      │      │ S5 模拟交易     │──────────┐
                     │ Valuation    │      │ Paper Exec     │          │
                     │ (System A)   │      │ 开仓+持仓+监控  │          ▼
                     │ 被S3当工具调  │      │ 到目标/止损/到期│   (写回 S2)
                     └─────────────┘      └───────┬────────┘
                                                   │ 同一接口
                                          ┌────────▼────────┐
                                          │ S6 真实交易(未来) │
                                          │ Live Exec (IBKR) │
                                          └─────────────────┘
        横切: 通知 Notification · 监控绩效 Monitoring(ledger/backtest) · 编排 Orchestration
```

## 2. 子系统职责表

| 子系统 | 职责 | 输入 → 输出 | 现状 / 落点（package） |
|---|---|---|---|
| **S1 采集 Ingestion** | 从外部源拉原始事件，归一化 + 去重 | 外部源 → **Event** | 少量已有(news refresh)；新建 `service/ingestion/`，`EventSource` 接口 |
| **S2 数据层 Data Layer** | **只存取、无业务逻辑**的共享地基 | 各系统读写 | 已有 `data/`+`models/`+`db.py`，迁 Neon |
| **S3 信号分析 Analysis** | 收 Event → 拉事实 → Agent 读懂事件、调假设、重定价 → 产出 Trading Signal | Event + facts → **Trading Signal** | **全新核心** = `signal_system/`(System B) + Agent Core |
| **S4 估值 Valuation** | "值多少钱"，被 S3 当工具调 | facts → 公允价值 | 已有 `valuation/`(System A) |
| **S5 模拟交易 Paper Exec** | 拿 Trading Signal 自动模拟开仓、维护持仓、到目标/止损/到期自动平仓、记盈亏 | Trading Signal → fills/PnL | **全新** `execution/`(paper 实现)；Signal Monitor 归此 |
| **S6 真实交易 Live Exec** | 同 S5 接口，接券商 API（IBKR） | Trading Signal → 真实下单 | **未来**；与 S5 共享 `ExecutionEngine` 接口 |
| 通知 Notification | 推送 Trading Signal + 成交/止损事件 | 事件 → 邮件/IM | 新建 `service/notify/` |
| 监控绩效 Monitoring | 每条信号前向收益 / scorecard / LLM vs Algo 对比 | fills → 评分 | 复用 `ledger/` + `backtest/` |
| 编排 Orchestration | 调度 poller、串 S1→S3→S5 事件流 | — | 新建 `service/` 主循环 |

## 3. 子系统间消息契约（边界的本体）

> 这些契约是 Modular Monolith 的命脉。先把它们定死，子系统在不在一个进程里都无所谓。
> 字段为设计草案，开发时以代码 schema 为准。

### Event（S1 → S2 → S3）
```
Event {
  id, source, external_id,       # 去重键 (source, external_id)
  symbol, event_type,            # grade_change | m&a | insider | earnings | ...
  raw,                           # 原始 payload
  observed_at, ingested_at,
}
```

### TradingSignal（S3 → S2 → S5）
```
TradingSignal {
  id, event_id, symbol,
  direction,                     # buy | sell | hold
  target_price, stop_loss, horizon_days,   # ← 四个核心要素
  conviction,                    # 仅强度/通知优先级，不参与仓位
  fair_value_base,               # 来自 S4，可空
  thesis, generated_by,          # llm | algo
  snapshot_id, created_at, expires_at,
  status,                        # open | target_hit | stopped_out | expired | closed
}
```

### ExecutionEngine 接口（S5 / S6 共用）
```
interface ExecutionEngine:
    open(signal: TradingSignal) -> Position
    monitor(position) -> 触发 target_hit / stopped_out / expired 时平仓
    close(position, reason) -> Fill
```
- `PaperExecutionEngine`（S5，现在）与 `LiveExecutionEngine`（S6，未来）是两个实现。
- 信号在模拟盘验证够了，切真实盘**几乎零改动**——这是"先模拟后真实"能落地的关键。

## 4. 已确认的演进点

1. **新增 S5 自动模拟交易**：从原方案"通知人 → 人手动下单"演进为"**自动模拟执行**"。
   好处：自动 forward-test 每条信号有没有用，配合 Monitoring 持续打分。
2. **S5/S6 共享 `ExecutionEngine` 接口**：paper 与 live 仅是实现差异。
3. **S1 多事件源插拔**：每类事件一个 `EventSource` 适配器，对应真实 FMP 端点——
   评级 `analyst/grades`、新闻 `news`、insider `insiderTrades`、机构持仓 `form13F`。

## 5. 包结构落点（建议）

```
quant_researcher/
  data/ models/ db.py        # S2 数据层（已有）
  valuation/                 # S4 估值 / System A（已有）
  ledger/ backtest/          # 监控绩效（已有，复用）
  signal_system/             # S3 信号分析 / System B（新）
  execution/                 # S5/S6 执行（新；paper + live 实现）
  service/
    ingestion/               # S1 采集（新）
    agent/                   # S3 的 Agent Core（新）
    notify/                  # 通知（新）
    api.py                   # FastAPI / REST + webhook
    main.py                  # S 编排主循环
```

跨子系统**只经接口/契约调用**，不 import 对方内部模块。现有 `qr` CLI 与 JSON envelope 契约保持不动。
