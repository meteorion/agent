# 版本矩阵

> 每节"实测跑通"的版本组合。环境变了先查这张表。

| 小节 | 验证过的版本组合 | 验证日期 |
|---|---|---|
| 1.1 | uv 0.11.31 · Python 3.12.13 · pydantic 2.14.3 · pydantic-settings 2.16.3 · python-dotenv 1.2.4 · pytest 8.4.2 · pytest-asyncio 1.5.0 · ruff 0.17.5 · Windows 11 | 2026-11-17 |
| 1.2 | 同 1.1（无新增依赖）；新增 `pythonpath = ["."]` 于 pytest 配置；要求 Python ≥3.11（`TaskGroup`/`except*`/`asyncio.timeout`） | 2026-11-17 |
| 1.3 | 新增 **httpx 0.28.1**（含 httpcore 1.0.9 / h11 0.16.0 / certifi 2026.10.23 / idna 3.20）；其余同 1.1。**仅对本地 mock 验证，真实 API 路径未验证**（无 Key） | 2026-11-17 |
| 1.4 | 无新增依赖（复用 pydantic 2.14.3）；纯本地，不调 LLM | 2026-11-17 |
| 1.5 | 新增 **structlog 26.1.0**；其余同 1.3。测试注入 `FakeSleep`，36 个用例 7.3s 跑完 | 2026-11-17 |
| 1.6 | 无新增依赖；语料 `knowledge/` 2 篇共 4002 字符；49 个用例全绿 | 2026-11-17 |
| 1.7 | 新增 **qdrant-client 1.20.0**（含 numpy 2.5.5 / protobuf 7.36.2 / portalocker 4.4.2 / urllib3 2.8.0）；local 模式 `:memory:` 无需服务端；mock 新增 `/v1/embeddings`；64 个用例全绿 | 2026-11-17 |
| 1.8 | 无新增依赖；mock 新增"RAG 应答"行为（识别【资料】/【问题】标记）；78 个用例全绿；**阶段一 / M1 完成** | 2026-11-17 |
| 2.1 | 无新增依赖；`LLMClient.chat` 增加 `tools`/`tool_choice`，`ChatResult` 增加 `tool_calls`；mock 支持工具调用往返；94 个用例全绿 | 2026-11-17 |
| 2.2 | 无新增依赖、无网络；新增文档 `docs/mcp-notes.md` 与决策器；105 个用例全绿 | 2026-11-17 |
| 2.3 | 无新增依赖、无网络；新增 `docs/mcp-stateless.md` 与 `mcp/envelope.py`；依据官方 changelog / versioning / blog 校正字段名；125 个用例全绿 | 2026-11-17 |
| 2.4 | 无新增依赖；新增 `mcp/server.py`（stdio）；`configure_logging` 增加 `stream` 参数（MCP Server 必须写 stderr）；真实子进程 stdio 往返验证通过；145 个用例全绿 | 2026-11-17 |
| 2.5 | 无新增依赖（标准库 `ThreadingHTTPServer` + httpx）；新增 `mcp/http_app.py` 与 `Dockerfile`；两实例并发与跨实例句柄验证通过；160 个用例全绿。⚠️ `Dockerfile` 未实际构建（本机无 Docker） | 2026-11-17 |
| 2.6 | 无新增依赖；新增 `mcp/auth.py`、`create_ticket` 工具（含 MRTR）、`scripts/mrtr_client.py`；工具数 3→4；180 个用例全绿 | 2026-11-17 |
| 2.7 | 新增 **langchain-mcp-adapters 0.3.2**（含 mcp 1.30.0 / langchain-core 1.8.1）；Server 增加 dual-era legacy 支持；新增 `search_knowledge` 工具（工具数 4→5）；189 个用例全绿；**阶段二 / M2 完成** | 2026-11-17 |
| 3.1 | 新增 **langgraph 1.2.22**（含 langgraph-checkpoint 4.3.2 / prebuilt 1.1.2 / sdk 0.4.14）；**版本与大纲假设的 0.6.x 不一致**，API 形状一致；201 个用例全绿 | 2026-11-18 |
| 3.2 | 无新增依赖；新增 `graph/state.py`（三类 reducer + 派生函数）；217 个用例全绿 | 2026-11-18 |
| 3.3 | 无新增依赖；新增 `graph/edges.py`（条件边 / 自环 / 兜底 / Command 两版）；实测默认递归上限 10007；235 个用例全绿 | 2026-11-18 |
| 3.4 | 新增 **langgraph-checkpoint-sqlite 3.1.3**（含 aiosqlite 0.22.1）；真杀进程（`os._exit`）恢复验证通过；实测中断节点副作用执行 2 次；245 个用例全绿 | 2026-11-18 |
| 3.5 | 无新增依赖；新增 `graph/approval.py`（interrupt 审批门禁）；子进程挂起+新进程恢复验证通过；实测被 resume 节点重跑导致副作用 2 次；256 个用例全绿 | 2026-11-18 |
| 3.6 | 无新增依赖；新增 `graph/streaming.py`（两种流 + 扁平事件 + trace_id）；实测 15s 任务首条进度 0.00s；266 个用例全绿 | 2026-11-18 |
| 3.7 | 无新增依赖；新增 `graph/composite.py`（两个子图 + 两种合成）；**M3 完成**；实测子图内 interrupt 恢复会重跑前置节点；278 个用例全绿 | 2026-11-18 |
| 4.1 | 无新增依赖；新增 `multiagent/single_vs_multi.py`（单图 vs 多 Agent 跑分 + 工具上下文对比）；实测延迟 1.75x、schema 省 61.1%；修复 404/401 未排空请求体导致的偶发 `ReadError`；289 个用例全绿 | 2026-11-18 |
| 4.2 | 无新增依赖（httpx + 标准库）；新增 `a2a/server.py` 与 `a2a/client.py`；**方法名按 v1.0 用 `message/send`**（旧名 `tasks/send` 兼容）；子进程 Server + 轮询/取消/终态保护验证通过；303 个用例全绿 | 2026-11-18 |
| 4.3 | 无新增依赖；新增 `multiagent/topologies.py`（supervisor vs handoff）；实测轮次 2.0 倍、handoff 边表为空、踢皮球止损；319 个用例全绿 | 2026-11-18 |
| 4.4 | 无新增依赖；新增 `memory/short_term.py`（token 预算 + 约束抽取 + 三种策略）；实测 window/summary 各丢 2 个约束、hybrid 0 丢失；339 个用例全绿 | 2026-11-18 |
| 4.5 | 无新增依赖（复用 `HashEmbedder`）；新增 `memory/long_term.py`（语义/情景 + 打分召回 + 遗忘）；实测 90 天前工单排第 1、TTL 过期不被召回；355 个用例全绿 | 2026-11-18 |
| 4.6 | 无新增依赖；新增 `context/hygiene.py`（4 过滤器 + 事务报告）；实测裸拼含 4 类污染、叠加后拦下 4 条且冲突显式化；370 个用例全绿 | 2026-11-18 |
| 4.7 | 无新增依赖；新增 `memory/stress_test.py`（50 轮压测 + 四组配置）；实测 window 丢光 4 约束、hybrid 4/4 且省 80%、full 拦 2/3 污染；**M4 完成**；385 个用例全绿 | 2026-11-18 |
| 5.1 | 无新增依赖；新增 `eval/{dataset,judge,baseline}.py`（20 条评测集 + 规则判分 + 一致性）；实测基线 7/20、kappa 0.77；修正 `test_client_resilience` 跨模块计数器 flake；414 个用例全绿 | 2026-11-18 |
| 5.2 | 新增 **ragas 0.4.4**（安装成功但离线不可跑，已记录）；新增 `eval/rag_eval.py`（四项确定性代理指标 + 病根定位）；四套配置诊断全部命中；436 个用例全绿 | 2026-11-18 |
| 5.3 | 尝试 **deepeval 4.3.4**（全部指标需 API key，实测后移除依赖）；新增 `eval/agent_eval.py`（五条确定性判据 + CI 门禁）；实测拦住 5 类失败、修复版通过门禁；457 个用例全绿 | 2026-11-18 |
| 5.4 | 无新增依赖；新增 `eval/ci_gate.py` + `eval/baseline.json` + `.github/workflows/eval.yml`；实测退出码 0/1/2 三态；475 个用例全绿 | 2026-11-18 |
| 5.5 | 无新增依赖；新增 `obs/tracing.py` + `scripts/{trace_service,tracing_demo}.py`；给 `mcp/http_app.py` 加 span 收口；实测跨三进程 + MCP 调用同一条 trace；502 个用例全绿 | 2026-11-18 |
| 5.6 | 无新增依赖；新增 `guardrails/pipeline.py`（3 输入 + 5 输出规则、BLOCK/REWRITE、体面话术）；实测输入输出误杀均为 0；536 个用例全绿 | 2026-11-18 |
| 5.7 | 无新增依赖；新增 `resilience/resilience.py`（令牌桶 + 三态熔断 + 成本闸 + 降级链）；实测并发 10 请求只打下游 3 次、冷却后自动恢复；555 个用例全绿 | 2026-11-18 |
| 5.8 | 无新增依赖；新增 `ops/launch_checklist.py`（10 自动 + 6 人工检查、演练开关）；实测自动项 10/10、人工未签字正确阻塞、三场景演练均被拦下；**M5 完成**；571 个用例全绿 | 2026-11-18 |
| 收尾 | 全书体检（37 节 frontmatter / 修订记录 / produces / 链接全部通过；术语表去重 2 条）；新增 `tutorial/00-paths.md`（两种读法）与 `scripts/run_all.py`（一条命令跑 35 demo + 全部测试）；修掉 2.4/2.5 demo 里遗留的「旧版本号必须被拒」断言（2.7 dual-era 后不再成立）；580 个用例全绿 | 2026-11-18 |
