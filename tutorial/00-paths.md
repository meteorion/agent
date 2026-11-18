# 学习路径：两种读法，一套材料

> 37 节、5 个阶段、约 **3,900 分钟**（65 小时）的完整工程课。
> 你可以按**新手顺序**从头读，也可以按**问题**直接跳。

## 一、先确认你要哪种读法

| 你的情况 | 建议路径 |
|---|---|
| 从没做过 Agent，想系统学一遍 | **[路径 A：线性通关](#路径-a线性通关)** —— 按 1.1 → 5.8 走 |
| 已经会写 Agent，想解决具体问题 | **[路径 B：按问题跳查](#路径-b按问题跳查)** —— 从下面的问题索引找入口 |
| 只想看某个阶段 | 直接跳阶段开头那节，每节"前置检查清单"会告诉你缺什么 |
| 要评审一份 Agent 方案 | [上线检查单](05-eval-ops/05-08-launch-checklist.md) + [评测集设计](05-eval-ops/05-01-eval-dataset.md) |

**依赖关系一句话**：阶段一是一切的地基；阶段二依赖一；阶段三依赖二；阶段四依赖三；阶段五依赖一（不强依赖三四）。

---

## 路径 A：线性通关

### 阶段一 · 基础（约 1,000 分钟）

目标是**让一个 Agent 真的能跑**。

| 节 | 你会得到什么 |
|---|---|
| [1.1 工程骨架](01-foundation/01-01-project-skeleton.md) | 目录、pyproject、可运行的入口 |
| [1.2 async 模型](01-foundation/01-02-async-model.md) | 为什么 Agent 必须异步 |
| [1.3 亲手发请求](01-foundation/01-03-raw-llm-request.md) | 不借 SDK，看清 HTTP 细节 |
| [1.4 结构化输出](01-foundation/01-04-structured-output.md) | pydantic 校验与重试 |
| [1.5 重试与错误分层](01-foundation/01-05-error-taxonomy.md) | 可重试 / 需人工 / 致命 |
| [1.6 工具调用](01-foundation/01-06-tool-calling.md) | 工具注册表与 schema |
| [1.7 RAG](01-foundation/01-07-rag-basics.md) | 切分、嵌入、检索、重排 |
| [1.8 评测意识](01-foundation/01-08-eval-minded.md) | 证据门与引用校验 |

### 阶段二 · 协议（约 720 分钟）

目标是**把工具变成可复用的服务**。

| 节 | 你会得到什么 |
|---|---|
| [2.1 Function Calling](02-protocol/02-01-function-calling.md) | 工具循环的完整实现 |
| [2.2 为什么需要 MCP](02-protocol/02-02-why-mcp.md) | 协议栈与版本 |
| [2.3 MCP 无状态改造](02-protocol/02-03-mcp-stateless-migration.md) | session → 逐请求元数据 |
| [2.4 第一个 Server](02-protocol/02-04-mcp-server-discover.md) | 三原语 + stdio |
| [2.5 Streamable HTTP](02-protocol/02-05-mcp-streamable-http.md) | 单端点 + 订阅流 + Serverless |
| [2.6 MRTR + OAuth](02-protocol/02-06-mcp-mrtr-oauth.md) | 缺信息 vs 缺权限 |
| [2.7 接回 Agent（M2）](02-protocol/02-07-mcp-adapters-bridge.md) | 真实适配器 + 命名空间 + 白名单 |

### 阶段三 · 编排（约 675 分钟）

目标是**把流程变成图**。

| 节 | 你会得到什么 |
|---|---|
| [3.1 从 if 到状态图](03-graph/03-01-from-if-to-graph.md) | 手写编排的三种崩法 |
| [3.2 State 设计](03-graph/03-02-graph-state-design.md) | reducer、默认覆盖、派生字段 |
| [3.3 条件边与循环](03-graph/03-03-conditional-edges-loops.md) | 分支、重试、刹车、兜底 |
| [3.4 断点续跑](03-graph/03-04-checkpointer-resume.md) | 真杀进程再恢复 |
| [3.5 人工审批](03-graph/03-05-human-approval-interrupt.md) | interrupt 挂起与恢复 |
| [3.6 流式与追踪](03-graph/03-06-streaming-tracing.md) | 15 秒任务的首条进度 |
| [3.7 子图与 M3](03-graph/03-07-subgraph-main-graph.md) | 子图拆分 + 验收 |

### 阶段四 · 多 Agent（约 760 分钟）

目标是**先量再拆**。

| 节 | 你会得到什么 |
|---|---|
| [4.1 先量再拆](04-multi-agent/04-01-multi-agent-decision.md) | 四个指标 + 该不该拆 |
| [4.2 A2A 协议](04-multi-agent/04-02-a2a-protocol.md) | 跨进程协作 |
| [4.3 两种拓扑](04-multi-agent/04-03-supervisor-handoff.md) | supervisor vs handoff |
| [4.4 短期记忆](04-multi-agent/04-04-short-term-memory.md) | 压缩与约束保留 |
| [4.5 长期记忆](04-multi-agent/04-05-long-term-memory.md) | 语义/情景 + 遗忘 |
| [4.6 上下文污染](04-multi-agent/04-06-context-pollution.md) | 四种污染与四种解法 |
| [4.7 长对话压测（M4）](04-multi-agent/04-07-soak-test.md) | 50 轮后的成本与保留率 |

### 阶段五 · 上线（约 750 分钟）

目标是**从"能跑"到"敢上"**。

| 节 | 你会得到什么 |
|---|---|
| [5.1 评测集](05-eval-ops/05-01-eval-dataset.md) | golden set 与判分器 |
| [5.2 RAG 指标](05-eval-ops/05-02-ragas-eval.md) | 四项指标 + 病根定位 |
| [5.3 行为与安全](05-eval-ops/05-03-deepeval-agent.md) | 工具/越权/注入/PII |
| [5.4 CI 门禁](05-eval-ops/05-04-deepeval-ci-gate.md) | 回退就让流水线变红 |
| [5.5 全链路追踪](05-eval-ops/05-05-w3c-tracing.md) | traceparent 串起多进程 |
| [5.6 护栏](05-eval-ops/05-06-guardrails.md) | 输入输出双向拦截 |
| [5.7 韧性](05-eval-ops/05-07-circuit-breaker.md) | 熔断/限流/成本闸/降级 |
| [5.8 上线检查单（M5）](05-eval-ops/05-08-launch-checklist.md) | 16 项检查与演练 |

---

## 路径 B：按问题跳查

### "我的 Agent 答得不对"

| 症状 | 去这里 |
|---|---|
| 检索没找到 / 找到一堆噪声 | [5.2 RAG 指标](05-eval-ops/05-02-ragas-eval.md)（按指标组合定位病根） |
| 上下文里混进别人的信息 | [4.6 上下文污染](04-multi-agent/04-06-context-pollution.md) |
| 多轮之后忘掉关键约束 | [4.4 短期记忆](04-multi-agent/04-04-short-term-memory.md) |
| 上下文太长、成本太高 | [4.7 长对话压测](04-multi-agent/04-07-soak-test.md) |
| 编造了资料里没有的内容 | [1.8 评测意识](01-foundation/01-08-eval-minded.md) + [5.2](05-eval-ops/05-02-ragas-eval.md) |

### "我的 Agent 行为不对"

| 症状 | 去这里 |
|---|---|
| 工具选错 / 该调没调 | [5.3 行为与安全](05-eval-ops/05-03-deepeval-agent.md) |
| 越权执行（没审批就干） | [2.6 MRTR + OAuth](02-protocol/02-06-mrtr-oauth.md) + [3.5 审批](03-graph/03-05-human-approval-interrupt.md) |
| 被提示词注入带走 | [5.6 护栏](05-eval-ops/05-06-guardrails.md) |
| 泄漏别人的信息 | [5.3](05-eval-ops/05-03-deepeval-agent.md) + [5.6](05-eval-ops/05-06-guardrails.md) |

### "流程控制不住"

| 症状 | 去这里 |
|---|---|
| 一长串 if-else 越来越乱 | [3.1 从 if 到状态图](03-graph/03-01-from-if-to-graph.md) |
| 崩了要重跑、白花钱 | [3.4 断点续跑](03-graph/03-04-checkpointer-resume.md) |
| 需要人工审批 | [3.5 人工审批](03-graph/03-05-human-approval-interrupt.md) |
| 循环停不下来 / 无限重试 | [3.3 条件边与循环](03-graph/03-03-conditional-edges-loops.md) |
| 图越来越大、想拆 | [3.7 子图拆分](03-graph/03-07-subgraph-main-graph.md) |

### "不知道该不该拆多 Agent"

| 问题 | 去这里 |
|---|---|
| 要不要拆？ | [4.1 先量再拆](04-multi-agent/04-01-multi-agent-decision.md)（四个指标） |
| 拆成什么形状？ | [4.3 两种拓扑](04-multi-agent/04-03-supervisor-handoff.md) |
| 跨团队/跨厂商怎么协作？ | [4.2 A2A 协议](04-multi-agent/04-02-a2a-protocol.md) |

### "要上线了，心里没底"

| 担心 | 去这里 |
|---|---|
| 改坏了怎么办 | [5.4 CI 门禁](05-eval-ops/05-04-deepeval-ci-gate.md) |
| 出问题查不到是哪一跳 | [5.5 全链路追踪](05-eval-ops/05-05-w3c-tracing.md) |
| 下游挂了会雪崩 | [5.7 韧性](05-eval-ops/05-07-circuit-breaker.md) |
| 账单会爆炸 | [5.7 成本闸](05-eval-ops/05-07-circuit-breaker.md) |
| 整体能不能上 | [5.8 上线检查单](05-eval-ops/05-08-launch-checklist.md) |

---

## 配套代码怎么跑

全部代码在 `servicedesk-agent/`。一条命令跑完全部验证：

```powershell
cd servicedesk-agent
uv run python -m scripts.run_all          # 571 个测试 + 12 个 demo
uv run python -m scripts.run_all --fast   # 跳过耗时 demo（15s / 3s 那种）
```

单个 demo 想跑哪个就 `uv run python -m scripts.<名字>`，章节里都写了命令。

---

## 附录（随时查）

- [术语表](appendix/glossary.md) —— 269 条，按章节编号
- [错误案例库](appendix/errors.md) —— 138 条"现象 → 根因 → 修法"
- [版本记录](appendix/versions.md) —— 每节的真实依赖版本与实测结论
- [总目录](00-index.md) —— 37 节进度与一句话摘要
