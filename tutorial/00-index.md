# 2026 AI Agent 零基础到生产级开发实战教程

> 人读入口就是本文件；机器读入口是 `.state/progress.json`。
> 标记区内的进度表由脚本同步，**不要手改**，手改会在下次 `set` 时被覆盖。

## 怎么续跑

```bash
python .dsh/skills/agent-tutorial/scripts/tutorial.py status      # 看进度与下一节
python .dsh/skills/agent-tutorial/scripts/tutorial.py start 3.4   # 摘出某节任务卡
python .dsh/skills/agent-tutorial/scripts/tutorial.py set 3.4 done
```

## 进度

<!-- PROGRESS:START -->
| 编号 | 小节 | 状态 | 难度 | 时长 | 里程碑 | 文件 |
|---|---|---|---|---|---|---|
| 1.1 | 从空目录建立可运行的工程骨架 | ● done | ★☆☆☆☆ | 45min | M1 | [01-01-project-skeleton.md](01-foundation/01-01-project-skeleton.md) |
| 1.2 | 用「点餐叫号」理解 async：Agent 为什么必须异步 | ● done | ★★☆☆☆ | 75min | M1 | [01-02-async-model.md](01-foundation/01-02-async-model.md) |
| 1.3 | 不借 SDK，亲手发一次 LLM 请求 | ● done | ★★★☆☆ | 90min | M1 | [01-03-raw-llm-request.md](01-foundation/01-03-raw-llm-request.md) |
| 1.4 | 用 pydantic v2 把一段文字变成结构化数据 | ● done | ★★☆☆☆ | 75min | M1 | [01-04-pydantic-structured-output.md](01-foundation/01-04-pydantic-structured-output.md) |
| 1.5 | 给 LLM 客户端装上超时、重试与异常分层 | ● done | ★★★☆☆ | 90min | M1 | [01-05-resilient-client.md](01-foundation/01-05-resilient-client.md) |
| 1.6 | 基础 RAG（一）：把文档切成检索友好的块 | ● done | ★★☆☆☆ | 75min | M1 | [01-06-rag-splitting.md](01-foundation/01-06-rag-splitting.md) |
| 1.7 | 基础 RAG（二）：embedding、向量库与检索 | ● done | ★★★☆☆ | 100min | M1 | [01-07-rag-embedding-retrieval.md](01-foundation/01-07-rag-embedding-retrieval.md) |
| 1.8 | 基础 RAG（三）：拼 prompt、给引用、压幻觉（M1 验收） | ● done | ★★★☆☆ | 90min | M1 | [01-08-rag-prompt-citations.md](01-foundation/01-08-rag-prompt-citations.md) |
| 2.1 | 用 Function Calling 让模型自己选工具 | ● done | ★★★☆☆ | 90min | M2 | [02-01-function-calling.md](02-protocol/02-01-function-calling.md) |
| 2.2 | 为什么需要 MCP：从 N×M 集成地狱到统一协议 | ● done | ★☆☆☆☆ | 45min | M2 | [02-02-why-mcp.md](02-protocol/02-02-why-mcp.md) |
| 2.3 | 2026-07-28 无状态改造：握手与 Session 去了哪 | ● done | ★★★★☆ | 90min | M2 | [02-03-mcp-stateless-migration.md](02-protocol/02-03-mcp-stateless-migration.md) |
| 2.4 | 用 server/discover + 三原语写第一个无状态 MCP Server | ● done | ★★★★☆ | 120min | M2 | [02-04-mcp-server-discover.md](02-protocol/02-04-mcp-server-discover.md) |
| 2.5 | Streamable HTTP + subscriptions/listen 上 Serverless | ● done | ★★★★★ | 120min | M2 | [02-05-mcp-streamable-http.md](02-protocol/02-05-mcp-streamable-http.md) |
| 2.6 | 用 MRTR + OAuth 2.0 处理「缺信息」与「缺权限」 | ● done | ★★★★☆ | 100min | M2 | [02-06-mcp-mrtr-oauth.md](02-protocol/02-06-mcp-mrtr-oauth.md) |
| 2.7 | 用 langchain-mcp-adapters 把 MCP 工具接进 Agent（M2 验收） | ● done | ★★★★☆ | 100min | M2 | [02-07-mcp-adapters-bridge.md](02-protocol/02-07-mcp-adapters-bridge.md) |
| 3.1 | 从「一长串 if」到状态图：为什么需要 LangGraph | ● done | ★★☆☆☆ | 75min | M3 | [03-01-from-if-to-graph.md](03-graph/03-01-from-if-to-graph.md) |
| 3.2 | 设计 State：什么才是「真状态」 | ● done | ★★★☆☆ | 90min | M3 | [03-02-graph-state-design.md](03-graph/03-02-graph-state-design.md) |
| 3.3 | 条件边与循环：让图会「回头看」 | ● done | ★★★☆☆ | 90min | M3 | [03-03-conditional-edges-loops.md](03-graph/03-03-conditional-edges-loops.md) |
| 3.4 | 用 Checkpointer 实现「杀进程后断点续跑」 | ● done | ★★★★☆ | 100min | M3 | [03-04-checkpointer-resume.md](03-graph/03-04-checkpointer-resume.md) |
| 3.5 | 用 interrupt() 做人工审批门禁 | ● done | ★★★★☆ | 100min | M3 | [03-05-human-approval-interrupt.md](03-graph/03-05-human-approval-interrupt.md) |
| 3.6 | 流式输出与可观测：把「图在干嘛」讲给用户 | ● done | ★★★☆☆ | 80min | M3 | [03-06-streaming-tracing.md](03-graph/03-06-streaming-tracing.md) |
| 3.7 | 子图拆分与 M3 端到端验收 | ● done | ★★★★☆ | 110min | M3 | [03-07-subgraph-main-graph.md](03-graph/03-07-subgraph-main-graph.md) |
| 4.1 | 先别急着多 Agent：什么时候单图就够 | ● done | ★★☆☆☆ | 60min | M4 | [04-01-multi-agent-decision.md](04-multi-agent/04-01-multi-agent-decision.md) |
| 4.2 | 用 A2A 协议做跨 Agent 协作 | ● done | ★★★★☆ | 110min | M4 | [04-02-a2a-protocol.md](04-multi-agent/04-02-a2a-protocol.md) |
| 4.3 | Supervisor 与 Handoff：两种多 Agent 拓扑 | ● done | ★★★★☆ | 100min | M4 | [04-03-supervisor-handoff.md](04-multi-agent/04-03-supervisor-handoff.md) |
| 4.4 | 双层记忆（一）：短期会话与压缩 | ● done | ★★★☆☆ | 90min | M4 | [04-04-short-term-memory.md](04-multi-agent/04-04-short-term-memory.md) |
| 4.5 | 双层记忆（二）：长期语义 + 情景记忆与遗忘 | ● done | ★★★★☆ | 110min | M4 | [04-05-long-term-memory.md](04-multi-agent/04-05-long-term-memory.md) |
| 4.6 | 上下文污染的根因与四种解法 | ● done | ★★★★☆ | 100min | M4 | [04-06-context-pollution.md](04-multi-agent/04-06-context-pollution.md) |
| 4.7 | 长对话压测与 M4 验收 | ● done | ★★★★☆ | 80min | M4 | [04-07-soak-test.md](04-multi-agent/04-07-soak-test.md) |
| 5.1 | 先建评测集：没有 golden set 就没有工程 | ● done | ★★★☆☆ | 90min | M5 | [05-01-eval-dataset.md](05-eval-ops/05-01-eval-dataset.md) |
| 5.2 | 用 RAGAS 给 RAG 打分并定位病根 | ● done | ★★★★☆ | 100min | M5 | [05-02-ragas-eval.md](05-eval-ops/05-02-ragas-eval.md) |
| 5.3 | 用 DeepEval 评测 Agent 行为与安全 | ● done | ★★★★☆ | 110min | M5 | [05-03-deepeval-agent.md](05-eval-ops/05-03-deepeval-agent.md) |
| 5.4 | 把评估接进 CI：指标回退就让流水线变红 | ● done | ★★★★☆ | 100min | M5 | [05-04-deepeval-ci-gate.md](05-eval-ops/05-04-deepeval-ci-gate.md) |
| 5.5 | 用 W3C traceparent 串起全链路 | ● done | ★★★★☆ | 100min | M5 | [05-05-w3c-tracing.md](05-eval-ops/05-05-w3c-tracing.md) |
| 5.6 | 护栏：输入输出双向拦截 | ● done | ★★★★☆ | 100min | M5 | [05-06-guardrails.md](05-eval-ops/05-06-guardrails.md) |
| 5.7 | 熔断、降级、限流与成本闸 | ● done | ★★★★☆ | 110min | M5 | [05-07-circuit-breaker.md](05-eval-ops/05-07-circuit-breaker.md) |
| 5.8 | 上线检查单与 M5 验收 | ● done | ★★★☆☆ | 90min | M5 | [05-08-launch-checklist.md](05-eval-ops/05-08-launch-checklist.md) |
<!-- PROGRESS:END -->

## 状态含义

| 状态 | 含义 |
|---|---|
| ○ planned | 只有标题，没有正文 |
| ◐ drafted | 正文写完，但代码没跑通 / 验收没验过 |
| ◑ verified | 代码跑通，真实输出已粘回正文 |
| ● done | 通过 16 项自检门禁 |

## 阶段范围与能力矩阵

<!-- 待补：每阶段 In-Scope / Out-Scope / 关键难点、C1–C7 覆盖矩阵、两条学习路线 -->

## 学习路线

- 最短可上线路径：1.1 → 1.3 → 1.5 → 1.6 → 1.7 → 1.8 → 2.1 → 2.2 → 2.4 → 2.7 → 3.1 → 3.2 → 3.4 → 3.5 → 3.7 → 5.1 → 5.3 → 5.4 → 5.6 → 5.7
- 完整体系路径：全部 37 节
