---
section: "2.7"
title: "用 langchain-mcp-adapters 把 MCP 工具接进 Agent（M2 验收）"
stage: 2
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M2
depends_on: ["2.6"]
produces: ["graph/tools_bridge.py"]
verifies: "Agent 能在一次回答里串起「查资产 → 查权限 → 给结论」三个 MCP 工具"
part: null
updated: "2026-11-17"
---

# 2.7 用 langchain-mcp-adapters 把 MCP 工具接进 Agent（M2 验收）

## 0. 本节导航

**本节在链路中的位置**：阶段二的收尾与 **M2 验收**。前面 6 节造好了协议、Server、鉴权；本节把它**接回 Agent**，让 2.1 的工具循环用上远端 MCP 工具。

**本节你将获得**

1. 用**真实** `langchain-mcp-adapters` 从 HTTP MCP Server 取工具，并接回 2.1 的 `ToolRegistry`。
2. 命名空间方案：`mcp__demo__query_asset`，解决多 Server 同名冲突。
3. **token 预算**：实测全量 5 个工具占 577 token，白名单 3 个降到 365（省 212）。
4. 一个真实生态发现：**`mcp 1.30.0` 仍走 legacy `initialize`（2025-11-25）**，我们不得不做 dual-era。
5. **M2 验收表**：一次回答串起查资产 → 查权限 → 知识库检索。

**前置检查清单**

- 2.6 的 `uv run python -m scripts.mrtr_client` 能跑通。
- 有网络（要装 `langchain-mcp-adapters`）。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（mock LLM，本地 MCP）

---

## 1. 为什么需要它（痛点先行）

2.6 结束时，你的 MCP Server 已经很完整：三原语、鉴权、MRTR。但**还没有任何一个 Agent 在用它**。

要接上，你必须自己写三件事：

1. **发现**：调 `tools/list`，把结果转成"模型能看懂的工具定义"
2. **转换**：MCP 的 `inputSchema` → LangChain 的 `args_schema`（或你自研框架的格式）
3. **代理调用**：模型说"调 `query_asset`" → 你发 `tools/call` → 把结果转回模型能读的文本

**这三件事每个框架都要写一遍**——这正是 `langchain-mcp-adapters` 存在的理由。
它把这层"胶水"做成了标准件。

但本节不只讲"装个包就完了"。因为真实世界里还有两个坑：

**坑 A：生态还在迁移中。** 2026-07-28 是"无状态"版，但不是所有客户端都跟上了。
实测：`langchain-mcp-adapters 0.3.2` + `mcp 1.30.0` 发起的第一条请求是

```json
{"method":"initialize","params":{"protocolVersion":"2025-11-25", ...}}
```

**它还在用握手制。** 而我们的 Server 只支持新版 → 直接 400。

**坑 B：工具一多，schema 就吃掉上下文。** 5 个工具的 schema 是 577 token。
接 20 个 Server、100 个工具就是上万 token——**在模型读到用户问题之前，上下文已经花掉一半**。

---

## 2. 核心概念与架构设计

### 2.1 类比：万能转换插头

你已经有一个标准插座（MCP Server），但手上的电器（LangChain Agent）是另一种插头。
**适配器**就是那个转接头：不改电器、不改插座，中间加一层。

> **这个类比在哪失效**：转接头只是物理转换，而 MCP 适配器还要**翻译语义**——
> 把 `inputSchema` 翻成 `args_schema`、把 content block 翻成纯文本。
> 翻译出偏差，模型就会选错工具或读不懂结果。

### 2.2 适配器做的三件事

```
MCP Server                          Agent
    │                                 │
    │ ① tools/list                    │
    │ ◄───────────────────────────────│  discover
    │                                 │
    │   {name, description,           │
    │    inputSchema}                 │
    │  ──────────────────────────────►│  ② 转成 BaseTool
    │                                 │     name/description/args_schema
    │ ③ tools/call {name, arguments}  │
    │ ◄───────────────────────────────│  模型决定调用
    │  ──────────────────────────────►│  ④ content blocks → 文本
```

### 2.3 我们要额外加的三层

| 层 | 问题 | 本节的解法 |
|---|---|---|
| **命名空间** | 两个 Server 都有 `search` | `mcp__demo__search` |
| **白名单** | 工具越多，选错率越高、schema 越贵 | `include=[...]` |
| **懒加载** | 可能只用到其中 3 个 | `ToolCatalog.discover()` → `load(names)` |

### 2.4 为什么必须做 dual-era

规范用三个词（2.3 §2）：

- **Modern**：2026-07-28，逐请求 `_meta`
- **Legacy**：2025-11-25 及更早，`initialize` 握手
- **Dual-era**：两者都支持

**过渡期（12 个月弃用窗口）里，你的 Server 应该是 dual-era。** 否则所有还没升级的客户端会在你发布的那一刻全部失效。

本节为此给 Server 加了最小 legacy 支持：接受 `initialize`、接受不带 `Mcp-Method` 的 `initialize` 与通知请求、并按旧形状返回（去掉 `resultType` / `ttlMs`）。

### 2.5 适用范围（边界）

| 情况 | 做法 |
|---|---|
| 工具数 < 10、单 Server | ✅ 直接全量加载 |
| 多 Server、工具 20+ | ✅ 命名空间 + 白名单 |
| 工具上百 | ✅ 必须先 `discover` 再按需 `load`，甚至按任务动态加载 |
| 只需要固定几个工具 | ⚠️ 别用适配器，直接调 `tools/call` 更省 |

---

## 3. 环境与版本

```powershell
uv add langchain-mcp-adapters
```

实测装出来的版本组合（这一组很重要）：

| 组件 | 实测版本 | 说明 |
|---|---|---|
| `langchain-mcp-adapters` | **0.3.2** | 提供 `MultiServerMCPClient` |
| `mcp` | **1.30.0** | 官方 SDK；**仍走 legacy 握手** |
| `langchain-core` | **1.8.1** | `BaseTool` / `StructuredTool` |
| Python | `3.12.13` | — |

**验证**

```powershell
uv run python -c "import importlib.metadata as m; print(m.version('langchain-mcp-adapters'), m.version('mcp'))"
```

```
0.3.2 1.30.0
```

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/tools_bridge.py` | **主角**：命名空间 / 白名单 / 转 registry |
| `src/servicedesk/tools/builtin.py` | 新增 `search_knowledge`（把 RAG 搬进 Server） |
| `src/servicedesk/mcp/{server,http_app}.py` | 加 dual-era legacy 支持 |
| `scripts/m2_agent.py` | M2 验收脚本 |
| `tests/test_tools_bridge.py` | 12 个断言 |

### 4.1 命名空间

```python
MCP_TOOL_SEP = "__"


def namespaced(server: str, tool: str) -> str:
    """`demo` + `query_asset` → `mcp__demo__query_asset`。

    为什么必须加前缀：两个 MCP Server 都暴露 `search` 时，裸名会互相覆盖——
    后注册的悄悄赢，没有任何报错。
    """
    return f"mcp{MCP_TOOL_SEP}{server}{MCP_TOOL_SEP}{tool}"


def split_namespaced(name: str) -> tuple[str | None, str]:
    parts = name.split(MCP_TOOL_SEP)
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[1], MCP_TOOL_SEP.join(parts[2:])
    return None, name
```

### 4.2 从 LangChain 工具取出参数模型（**有坑**）

```python
def _params_model(tool: Any) -> Any:
    """取得工具参数对应的 pydantic 模型。

    注意：`args_schema` 在 langchain-mcp-adapters 里是**JSON Schema 字典**，
    不是模型类；真正的模型要用 `get_input_schema()` 拿。
    """
    getter = getattr(tool, "get_input_schema", None)
    if callable(getter):
        model = getter()
        if hasattr(model, "model_validate"):
            return model
    raw = getattr(tool, "args_schema", None)
    if isinstance(raw, type) and hasattr(raw, "model_validate"):
        return raw
    return None
```

### 4.3 把工具接回 2.1 的循环

```python
    def to_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for full_name in self.names():
            tool = self._tools[full_name]
            info = self._infos[full_name]
            params_model = _params_model(tool)

            async def proxy(_tool=tool, **_kwargs: Any) -> Any:
                result = await _tool.ainvoke(_kwargs)
                return _normalize(result)

            registry.register(
                name=full_name,
                description=info.description,
                params_model=params_model,
                timeout_s=self.timeout_s,
            )(proxy)
        return registry
```

**注意 `to_registry()` 的存在意义**：它让 `run_tool_loop`（2.1 写的）**一行不改**就能用上远端工具。
工具来源从"本地函数"变成"HTTP 上的 MCP Server"，而循环本身不关心。

### 4.4 结果归一化

```python
def _normalize(result: Any) -> Any:
    if isinstance(result, list):
        texts = [item.get("text") for item in result
                 if isinstance(item, dict) and item.get("type") == "text"]
        if texts:
            joined = "\n".join(str(t) for t in texts)
            try:
                return json.loads(joined)
            except json.JSONDecodeError:
                return {"content": joined}
        return {"items": result}
    ...
```

MCP 返回的是 `[{"type":"text","text":"..."}]`。不拆开直接丢给模型，模型会看到
`{"items":[{"type":"text","text":"..."}]}` —— 多一层壳，白烧 token 还降低可读性。

### 4.5 运行

```powershell
uv run python -m scripts.m2_agent
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 `args_schema` 是 dict，不是模型

这是本节第一个坑。实测打印：

```
args_schema: <class 'dict'> {'description': '建单参数…', 'properties': {...}, 'required': ['title']}
```

我最初的代码用 `hasattr(params_model, "model_validate")` 判断，结果**所有工具都被跳过**，
registry 是空的——而日志级别是 `ERROR`，`log.warning` 没打出来，症状就是"模型一个工具都没调，直接瞎答"。

**修法**：用 `get_input_schema()`。它返回真正生成的 pydantic 模型类。

### 5.2 content block 必须拆

不拆的后果（实测第 4 步第一次跑）：

```
{"items": [{"type": "text", "text": "{\"found\": true, \"employee_id\": \"E-2048\"…"}]}
```

模型要在三层嵌套里抠 JSON。拆开之后：

```
{"found": true, "employee_id": "E-2048", "name": "张伟", "vpn": true, …}
```

### 5.3 命名空间与冲突

实测：不加命名空间时，两个 Server 都叫 `search` →

```
不加命名空间: 工具名重复：search
```

**我们的 `ToolRegistry` 会抛错（好事）**，但很多适配器用 `dict[name] = tool` 直接覆盖——
**后注册的悄悄赢，没有任何报错**。加上命名空间后：

```
['mcp__serverA__search', 'mcp__serverB__search']
```

### 5.4 白名单的 token 账

实测（5 个 MCP 工具）：

```
全量 schema 约 577 tokens
白名单 ['query_asset', 'query_permission', 'search_knowledge']
精简后 schema 约 365 tokens（省 212）
```

省 37%。而工具越多，这个比例越夸张——**100 个工具时，schema 本身就是几千 token，
且模型选错的概率显著上升。**

### 5.5 dual-era 的实际代价

为了兼容 legacy，`http_app` 里我加了三处判断：

```python
        legacy_ok = (
            body_method == "initialize"      # 握手
            or is_notification              # JSON-RPC 通知没有 id
            or version_header == "2025-11-25"
        )
        if not method_header:
            if not legacy_ok:
                return 400
            method_header = body_method
```

**第一次写的时候我把它写成了"没有 `Mcp-Method` 就放行"**，结果 2.5 的
`test_missing_mcp_method_header_is_rejected` 直接失败——**路由头校验被悄悄绕过了**。

这就是"兼容性"的真实成本：**每放宽一条，都要重新审视它是否削弱了原有的防线。**

### 5.6 `rounds == 2` 是什么意思

M2 实测 `rounds: 2`、三个工具、耗时 223 / 93 / 52ms。

- `rounds=2` 表示"一次工具往返 + 一次最终回答"，**不是**"调了三次模型"
- 三个工具在**同一轮**并行发出（2.1 的实现是顺序 await，所以是 223+93+52=368ms）

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：不做命名空间

```
不加命名空间: 工具名重复：search
```

如果用的是"字典覆盖"式实现（很常见），后果是：**B 的工具悄悄覆盖了 A 的同名工具**，
模型以为在调 A，实际调到 B，返回的数据完全不对，而日志里一切正常。

**命名空间的成本是每个工具名多 12 个字符；收益是"永远不会串台"。**

### 6.2 实验 B：全量加载 vs 白名单

| | 工具数 | schema token | 相对成本 |
|---|---|---|---|
| 全量 | 5 | **577** | 100% |
| 白名单 | 3 | **365** | 63% |

按每天 1 万次请求估算，**每次省 212 token ≈ 每天 212 万 token**。
而更要命的是**准确率**：工具越多，模型选错的概率越高（1.8 的"检索噪声"是同一个道理）。

> 所以白名单不是"优化"，是**正确性措施**。

---

## 7. 验证与预期输出

### 7.1 M2 验收脚本

```powershell
uv run python -m scripts.m2_agent
```

实测（节选）：

```
第 1 步：用真实适配器把 MCP 工具取下来
  拿到 5 个工具：['create_ticket', 'query_asset', 'query_permission', 'query_ticket', 'search_knowledge']
  类型：StructuredTool（LangChain BaseTool）

第 2 步：命名空间 + token 预算
  命名空间后：['mcp__demo__create_ticket', 'mcp__demo__query_asset', …]
  全量 schema 约 577 tokens
  白名单 ['query_asset', 'query_permission', 'search_knowledge']
  精简后 schema 约 365 tokens（省 212）

第 3 步：把 MCP 工具接回 2.1 的工具循环
  registry 里的工具：['mcp__demo__query_asset', 'mcp__demo__query_permission', 'mcp__demo__search_knowledge']

第 4 步：端到端问答
  问题：NB-1024 是谁在用？他有 VPN 权限吗？如果认证失败该怎么办？
  轮次：2
  调用工具：['mcp__demo__query_asset', 'mcp__demo__query_permission', 'mcp__demo__search_knowledge']
    - query_asset        [demo] ok  223ms
    - query_permission   [demo] ok  93ms
    - search_knowledge   [demo] ok  52ms
  摘要：{'rounds': 2, 'tools': [...], 'failed': [], 'cost_cny': 0.000209, ...}

第 5 步：M2 验收对照
  ✅ 串起 ≥3 个 MCP 工具   ['query_asset', 'query_permission', 'search_knowledge']
  ✅ 含查资产             query_asset
  ✅ 含查权限             query_permission
  ✅ 含知识库检索          search_knowledge
  ✅ 只用了白名单内的工具    []

  M2 验收：通过
```

### 7.2 测试

```powershell
uv run pytest
```

```
........................................................................ [ 76%]
.............................................                            [100%]
189 passed in 23.61s
```

---

## 8. M2 里程碑验收

| # | 验收项 | 证据 | 状态 |
|---|---|---|---|
| 1 | 能写出符合 2026-07-28 的无状态 Server | `server/discover` + 三原语（2.4） | ✅ |
| 2 | 用两次独立请求调通同一工具，无 Session | 2.5 两实例并发、跨实例句柄 | ✅ |
| 3 | 订阅流按类型 opt-in、未知类型不打断 | 2.5 实测 `accepted/rejected` | ✅ |
| 4 | 区分缺信息与缺权限 | 2.6 八步：200+`input_required` vs 403 | ✅ |
| 5 | MRTR 完整往返且无状态 | 2.6 同 id 重试 + `inputResponses` | ✅ |
| 6 | 真实适配器能加载工具 | 5 个 `StructuredTool` | ✅ |
| 7 | 命名空间 / 白名单 / token 预算 | 577 → 365 token | ✅ |
| 8 | **一次回答串起 3 个 MCP 工具** | 本轮 `rounds=2`、3 个工具全 ok | ✅ |

**M2 完成标志**：一个 Agent 通过 MCP 协议，用远端工具查到了资产、权限与知识库，并给出可追溯结论。

---

## 9. 生产环境踩坑实录

### 坑 1：适配器连不上，报 400 Bad Request

**现象**

```python
client = MultiServerMCPClient({"demo": {"url": url, "transport": "streamable_http"}})
await client.get_tools()
# httpx.HTTPStatusError: Client error '400 Bad Request'
```

**定位方法**

抓一下 SDK 到底发了什么：

```python
{"method":"initialize","params":{"protocolVersion":"2025-11-25", ...}}
# 而且没有 Mcp-Method 头
```

**根因**

`mcp 1.30.0` 仍走 legacy 握手，而 Server 是 modern-only。
**这不是你写错了，是生态还没迁完。**

**修法**

给 Server 加 dual-era 最小支持：接受 `initialize`、允许 legacy 请求不带 `Mcp-Method`、
按旧形状返回（去掉 `resultType` / `ttlMs`）。

**顺手加固**：用 `server/discover` 让客户端自己决定走哪条路；过渡期结束前按版本打点统计流量。

### 坑 2：`args_schema` 是 dict，工具全被跳过

**现象**

模型一个工具都不调，直接给出无关回答。registry 是空的，但**没有任何报错**。

**定位方法**

```python
print(type(tool.args_schema), tool.args_schema)[:80]
print([t.name for t in tool_registry.names()])
```

**根因**

`langchain-mcp-adapters` 把 `args_schema` 存成 **JSON Schema 字典**，
用 `hasattr(x, "model_validate")` 判断会全部失配，然后被 `continue` 跳过。

**修法**

用 `tool.get_input_schema()`。并**把 `log.warning` 的级别提到可见**——这次的教训是
"跳过时没有噪声，症状看起来像模型变笨了"。

### 坑 3：兼容 legacy 时顺手把路由头校验废了

**现象**

加完 dual-era，2.5 的 `test_missing_mcp_method_header_is_rejected` 挂了。

**根因**

我最初写的是"没有 `Mcp-Method` 就放行"，等于**为了兼容旧客户端，废掉了防路由欺骗的校验**。

**修法**

只在**真正的 legacy 流量**上放行：

```python
legacy_ok = body_method == "initialize" or is_notification or version_header == "2025-11-25"
```

**顺手加固**：每放宽一条校验，都要问"这条校验原本防的是什么？现在还有别的东西防它吗？"

---

## 10. 自检与作业

### 10.1 三个自检问题

<details>
<summary><b>Q1：为什么工具名要加 <code>mcp__server__</code> 前缀？</b></summary>

因为多个 MCP Server 可能有同名工具（都叫 `search`）。裸名合并时要么报"工具名重复"（我们的 registry），要么后注册的悄悄覆盖前一个（字典实现）——后者更危险，模型以为在调 A 实际调到 B。前缀的成本是每个名字多 12 字符，收益是永不串台。

</details>

<details>
<summary><b>Q2：为什么"全量加载工具"不只是费钱，还会降低准确率？</b></summary>

工具描述本身就是给模型的 prompt。5 个工具 577 token，20 个就上千。一方面消耗上下文预算，另一方面**候选越多、越容易选错**（和 1.8 里"检索到噪声"是同一个问题）。白名单是正确性措施，不只是成本优化。

</details>

<details>
<summary><b>Q3：为什么适配器连不上时要先怀疑"协议版本"，而不是自己写错了？</b></summary>

因为 MCP 正在从 legacy（握手）迁到 modern（逐请求元数据），生态是混的。实测 `mcp 1.30.0` 发的仍是 `initialize` + `protocolVersion: 2025-11-25`。先抓一次原始请求，比读十遍文档快。

</details>

### 10.2 动手任务：按任务动态加载工具

**要求**

1. 先用 `ToolCatalog.discover()` 拿到全部工具元信息（只有 name/description）。
2. 用一次**廉价的 LLM 调用**（不带 tools，只给工具清单文本）判断"这次任务需要哪几个工具"。
3. 只把选中的工具装进 registry，再跑 `run_tool_loop`。
4. 对比：动态加载 vs 全量加载的 schema token 与最终是否成功。

**验收标准**

- 三种问题（查资产类、知识库类、建单类）分别只加载 2–3 个工具。
- schema token 比全量低 30% 以上。
- 最终工具调用结果与全量加载一致。
- 补 3 个用例，原有 189 个仍全绿。

**提示**：这就是"工具路由（tool routing）"的雏形。注意第 2 步的成本要算进去——如果清单本身就很长，可能得不偿失。

### 10.3 拓展挑战

接入**第二个** MCP Server（比如把 1.8 的 `ask` 做成另一个 Server），验证：

1. 命名空间是否防止了串台；
2. 两个 Server 的工具能否在同一轮里被同时调用；
3. 其中一个 Server 挂掉时，另一个是否不受影响（提示：2.5 的"实例互不影响"）。

---

## 11. 本节小结与延伸

**三点回顾**

1. **适配器只做"发现/转换/代理"三件事**，但它让 2.1 的循环一行不改就用上了远端工具——这就是分层的价值。
2. **命名空间与白名单是正确性措施**，不是优化：同名会串台、工具多了会选错（577 → 365 token 省 37%）。
3. **生态还在迁移**：`mcp 1.30.0` 仍发 legacy `initialize`（实测），所以过渡期必须做 dual-era——但**每放宽一条校验都要重新审视它原本防什么**。

**已知限制（诚实记录）**

- **dual-era 是最小实现**：只支持 `initialize` 与"不带头的 legacy 请求"，没有完整 legacy 能力协商。
- **`search_knowledge` 用的是哈希向量**（1.7 的局限），不是真实 embedding。
- **M2 的 LLM 仍是 mock**：它按关键词选工具，验证的是"链路正确"，**不验证模型的选工具能力**（那要阶段五的评估）。
- **未接第二个 Server**（§10.3）。

**延伸阅读**

- `langchain-mcp-adapters`：https://github.com/langchain-ai/langchain-mcp-adapters
- LangChain `BaseTool`：https://python.langchain.com/docs/concepts/tools/
- MCP 与 LangChain 集成示例：https://modelcontextprotocol.io/docs/develop/build-client

---

## 修订记录

- 2026-11-17 初稿：M2 验收脚本与 12 个测试全部实机运行，§7 为真实输出
- 2026-11-17 发现并记录：`langchain-mcp-adapters 0.3.2` + `mcp 1.30.0` 仍发 legacy `initialize`（2025-11-25，无 `Mcp-Method`），Server 需 dual-era 才能连通
- 2026-11-17 修正：`args_schema` 在适配器里是 dict，改用 `get_input_schema()` 取 pydantic 模型（原实现导致工具全被跳过）
- 2026-11-17 修正：MCP 返回的 content blocks 需拆 `text` 再解析，否则模型看到三层嵌套
- 2026-11-17 修正：dual-era 初次实现放宽过度，废掉了 `Mcp-Method` 校验（2.5 的测试失败）；改为只放行真正的 legacy 流量
- 2026-11-17 说明：新增 `search_knowledge` 使工具数 4→5，已同步更新 2.1/2.4/2.5 相关断言
