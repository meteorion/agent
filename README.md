# 2026 AI Agent 零基础到生产级开发实战教程

> 一套**能跑**的 Agent 工程课：37 节、5 个阶段、580 个测试、35 个可运行 demo。
> 从"让一个 Agent 跑起来"讲到"敢把它放上线"。

**不是概念科普，是逐节可验证的工程记录。** 每一节都有：可运行代码 → 真实输出 → 踩坑与修法 → 自检作业。

---

## 这是什么

这个仓库有两部分，互为表里：

| 目录 | 内容 |
|---|---|
| [`tutorial/`](tutorial/00-index.md) | 37 节教程 + 3 个附录（术语表 / 错误库 / 版本记录）+ 学习路径 |
| [`servicedesk-agent/`](servicedesk-agent/) | 配套工程：136 个文件、580 个测试、35 个 demo，全部可运行 |

教程里每一段输出都是**真跑出来的**；每个"踩坑"都带着当时的报错原文。

---

## 快速开始

需要 [uv](https://github.com/astral-sh/uv) 与 Python 3.12。

```powershell
cd servicedesk-agent
uv sync                                   # 安装依赖

uv run pytest -q                          # 580 个测试（约 50 秒）
uv run python -m scripts.run_all --fast   # 35 个 demo + 测试（约 100 秒）
```

`run_all --fast` 会跳过两个十几秒的慢 demo（异步演示、流式演示）。

**全部验证零成本**——不需要任何 API key：LLM 由本地 mock server 提供，
向量检索用哈希嵌入 + 内存 Qdrant，所有外部依赖都被替身接住。

---

## 教程怎么读

两种读法，见 [`tutorial/00-paths.md`](tutorial/00-paths.md)：

- **线性通关**：1.1 → 5.8，约 65 小时
- **按问题跳查**：从"我答得不对 / 我行为不对 / 我流程控制不住 / 该不该拆多 Agent / 要上线了"五类症状直达章节

只想快速了解结论，看 [`tutorial/00-index.md`](tutorial/00-index.md) 的一句话摘要表。

---

## 五个阶段

| 阶段 | 主题 | 产出 |
|---|---|---|
| **一 基础** | 异步、结构化输出、重试分层、工具调用、RAG、证据门 | 能跑的 Agent |
| **二 协议** | MCP 无状态改造、stdio Server、Streamable HTTP、MRTR + OAuth、适配器 | 可复用的工具服务 |
| **三 编排** | 状态图、reducer、条件边、断点续跑、人工审批、流式、子图 | 可控的流程 |
| **四 多 Agent** | 先量再拆、A2A、两种拓扑、双层记忆、上下文卫生、50 轮压测 | 可扩展的结构 |
| **五 上线** | 评测集、RAG 指标、行为安全、CI 门禁、全链路追踪、护栏、韧性、检查单 | 敢上线的系统 |

每个阶段结束都有验收（M1–M5），验收标准写在对应章节里，全部有实测证据。

---

## 几个反常识的实测结论

这些是写教程时真正撞出来的，不是抄来的：

- **LangGraph 默认递归上限是 10007 步，不是 25** —— 靠它当刹车等于让账单先爆
- **子图内的 `interrupt` 恢复后，父图会重跑前置兄弟节点** —— `durability` 三档都改不了
- **`add_messages` 会把 dict 转成 `BaseMessage`** —— 按 dict 取值会 `TypeError`
- **`sqlite3` 版的 Checkpointer 不支持异步图** —— 必须用 `AsyncSqliteSaver`
- **RAGAS 与 DeepEval 都离线跑不了** —— 所有指标都要求 LLM，哪怕"精确匹配工具名"
- **安全门禁的假阳性比假阴性更致命** —— 把"正确拒绝"判成泄漏，门禁活不过两周
- **`split` 配取反字符类会切出标点** —— 写好的规则静默失效
- **摘要不等于保真** —— 比滑窗贵 2.7 倍的摘要，丢的约束一样多

完整清单见 [`tutorial/appendix/errors.md`](tutorial/appendix/errors.md)（138 条）。

---

## 技术栈（实测版本）

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.13 | — |
| uv | 0.11.x | 依赖与运行 |
| pydantic / pydantic-settings | 2.14.3 | 结构化输出与配置 |
| httpx | 0.28.1 | HTTP 客户端 |
| structlog | 26.1.0 | 结构化日志 + contextvars |
| qdrant-client | 1.20.0 | 向量库（`:memory:` 模式） |
| langgraph | 1.2.22 | 状态图编排 |
| langgraph-checkpoint-sqlite | 3.1.3 | 断点续跑 |
| langchain-mcp-adapters | 0.3.2 | MCP 工具接入 |
| pytest / pytest-asyncio | 8.4.2 / 1.5.0 | 测试 |

精确到每节引入的依赖见 [`tutorial/appendix/versions.md`](tutorial/appendix/versions.md)。

---

## 工程结构

```
servicedesk-agent/
├── src/servicedesk/
│   ├── llm/            LLM 客户端（重试、错误分层）
│   ├── tools/          工具注册表与调用循环
│   ├── rag/            切分、嵌入、检索、重排、证据门
│   ├── mcp/            MCP Server（stdio + Streamable HTTP）、鉴权、MRTR
│   ├── a2a/            A2A Server/Client
│   ├── graph/          状态图、条件边、检查点、审批、流式、子图
│   ├── multiagent/     拓扑对比、单图 vs 多 Agent 评测
│   ├── memory/         短期压缩、长期记忆、长对话压测
│   ├── context/        上下文卫生（四种污染与解法）
│   ├── eval/           评测集、判分器、RAG 指标、行为安全、CI 门禁
│   ├── guardrails/     输入输出双向护栏
│   ├── resilience/     熔断、限流、成本闸、降级
│   ├── obs/            W3C traceparent
│   └── ops/            上线检查单
├── scripts/            35 个可运行 demo + mock LLM server + run_all
├── tests/              580 个测试
├── eval/baseline.json  指标基线（改它要走 review）
└── .github/workflows/  CI：测试 + 评测门禁 + 报告归档
```

---

## 这个仓库的"诚实"约定

- **每个数字都是实跑的**，没跑过的会明确标 `⚠️ 待核实`
- **每处自我修正都记在章节的「修订记录」里**（全书 38 处）
- **已知限制单独成节**，不藏在正文里
- **没有真实 LLM**：所有模型行为由 mock 或确定性逻辑代替，`5.2`／`5.3` 的分数**不可与公开榜单对比**

---

## 许可证

[MIT](LICENSE) —— 可自由使用、修改、商用，保留版权声明即可。

如果你需要专利授权条款（例如公司内部推广），可以换成 Apache-2.0：

```powershell
# 换成 Apache-2.0 需要替换 LICENSE 全文，并把下面这行写进 README
# Licensed under the Apache License, Version 2.0
```
