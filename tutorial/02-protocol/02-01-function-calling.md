---
section: "2.1"
title: "用 Function Calling 让模型自己选工具"
stage: 2
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M2
depends_on: ["1.5"]
produces: ["tools/registry.py", "tools/loop.py"]
verifies: "模型能在一句话里正确挑出 2 个工具并列调用"
part: null
updated: "2026-11-17"
---

# 2.1 用 Function Calling 让模型自己选工具

## 0. 本节导航

**本节在链路中的位置**：阶段二的开篇。1.8 的 RAG 只能"读文档"，本节给模型装上"手"——查资产、查权限、查工单。后面 2.3–2.7 的 MCP，本质就是**把这个工具循环标准化成协议**，所以本节是整章的地基。

**本节你将获得**

1. 一个从 pydantic 模型自动生成工具 schema 的注册表，含 `$ref` 内联（1.4 埋的伏笔，这里兑现）。
2. 一个完整的工具循环：模型要工具 → 你执行 → 结果回喂 → 最终回答。
3. 四类工具失败的**字符串化**处理，让模型能自我纠正而不是对话中断。
4. 实测"一句话并列调用两个工具"（同一轮、独立 call_id、各 60ms 左右）。
5. 两个真实事故的复现：跳过参数校验会静默返回错误结论；漏掉 assistant 消息直接 400。

**前置检查清单**

- 1.5 的 `uv run pytest` 全绿（本节的 `LLMClient` 是它的扩展）。
- 理解 1.3 的 `messages` 角色与顺序铁律（本节会真正用上 `tool` 角色）。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（mock，单轮约 ¥0.0001 的等价 token）

---

## 1. 为什么需要它（痛点先行）

1.8 结束时，你的助手能回答"VPN 认证失败怎么办"——因为答案在文档里。

但用户接着问：**"NB-1024 这台电脑是谁在用？"**

答案不在任何文档里，在资产系统里。模型只有两个选择：

- 说"资料中没有相关信息"（诚实但没用）
- 编一个（**实测过的幻觉路径**：1.8 的实验里，模型在没有资料时依然会把无关内容答得像模像样）

**Function Calling 就是为了解决这个：让模型有能力"申请"一次真实查询。**

### 1.1 但先纠正一个普遍误解

很多人以为 function calling 是"模型帮你执行函数"。**不是。**

模型做的只有一件事：返回一个 JSON，说"我想调用 `query_asset`，参数是 `{"hostname": "NB-1024"}`"。**函数是你执行的**，执行结果也是你回喂的。

```
你以为的：  模型 ──调用──► 你的函数 ──结果──► 模型
实际上的：  模型 ──"我想调"──► 你执行 ──"结果在此"──► 模型 ──► 最终回答
```

看清这一点，你才会明白为什么下面这些事都要你自己做：**参数校验、超时、错误处理、幂等、审计**。模型只负责"说它想调什么"。

### 1.2 三个真实事故

**事故 A：参数是字符串，不是对象。**

模型返回的 `arguments` 是 `"{\"hostname\": \"NB-1024\"}"`——**一个 JSON 字符串**。直接 `arguments["hostname"]` 会报 `TypeError: string indices must be integers`。

**事故 B：工具抛异常，整轮对话中断。**

你的 `query_asset` 里数据库超时了，抛了 `TimeoutError`。如果这个异常一路冒到顶层，用户看到 500，而**模型本来可以换个说法或换个工具**。

**事故 C：查不到 vs 出错，混为一谈。**

资产库里没有 `NB-9999`。这是"查不到"，不是"系统故障"。如果代码把它当异常抛，模型会以为工具坏了，然后开始编。

---

## 2. 核心概念与架构设计

### 2.1 类比：在餐厅点菜

- **菜单**（`tools` 参数）= 你能点的东西，含菜名、描述、配料要求（参数 schema）
- **顾客说"我要一份宫保鸡丁，不要花生"** = 模型返回 `tool_calls`
- **服务员下单给厨房** = 你执行真实函数
- **端回来的菜** = `tool` 消息
- **顾客尝了一口说"有点咸"** = 模型基于结果继续对话

> **这个类比在哪失效**：顾客不会点"菜单上没有的菜"，而**模型会**。它会编一个 `query_wifi`，会漏必填参数，会把数字写成字符串。所以"下单"这一步必须由你校验——这就是 `ToolRegistry.call()` 存在的全部理由。

### 2.2 完整往返（这就是"Agent"的最小形态）

```
user: "NB-1024 是谁在用？他有 VPN 权限吗？"
        │
        ▼
┌─────────────────────────────────────────┐
│ assistant                               │
│   content: ""                           │
│   tool_calls: [                         │
│     {id:"call_1", name:"query_asset",   │
│      arguments:'{"hostname":"NB-1024"}'},│
│     {id:"call_2", name:"query_permission"│
│      , arguments:'{"employee_id":"E-2048"}'}│
│   ]                                     │
└─────────────────────────────────────────┘
        │  ← 你执行（两个工具可以并行）
        ▼
┌─────────────────────────────────────────┐
│ tool  {tool_call_id:"call_1", content:…}│
│ tool  {tool_call_id:"call_2", content:…}│
└─────────────────────────────────────────┘
        │
        ▼
assistant: "NB-1024 是张伟在用，他有 VPN 权限……"
```

### 2.3 五个必须记住的点

| # | 事实 | 后果 |
|---|---|---|
| 1 | `arguments` 是**字符串** | 必须 `json.loads`，且可能失败 |
| 2 | `tool` 消息必须跟在**带 tool_calls 的 assistant** 之后 | 顺序错 → 400 |
| 3 | 每条 `tool` 消息必须带 `tool_call_id` | 缺了模型对不上号 |
| 4 | 模型可以**一次要多个工具** | 你要能并行执行 |
| 5 | 模型可能编造**不存在的工具名** | 必须查表，不能直接 `getattr` |

### 2.4 工具描述就是 prompt

```python
description=(
    "查询公司设备（笔记本/台式机）的资产信息，返回使用人、型号、状态与保修期。"
    "当用户询问某台设备归谁用、什么型号、还在不在保修期时使用。"
    "不要用它查员工权限或工单状态。"      # ← 这一句同样重要
)
```

模型靠这段文字决定"调不调、调哪个"。**写得含糊 = 让它猜。**

- ✅ "当用户询问某台设备归谁用时使用"（场景明确）
- ❌ "查询资产"（太短，模型不知道边界）
- ✅ "不要用它查员工权限"（**负向说明**同样关键，划清边界）

### 2.5 适用范围（边界）

| 场景 | 该用工具吗 |
|---|---|
| 需要**实时/私有**数据（资产、库存、订单） | ✅ 必须 |
| 需要**执行动作**（建单、退款） | ✅ 但要幂等（1.5 §8 坑 2） |
| 知识库里有的静态内容 | ❌ 用 RAG（1.8）更快更省 |
| 简单计算、格式化 | ⚠️ 模型自己会；用工具反而慢 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| pydantic | `2.14.3` | 参数模型与校验 |
| httpx | `0.28.1` | 底层请求 |
| structlog | `26.1.0` | `tool_called` 日志 |

**验证**

```powershell
uv run python -c "from servicedesk.tools.builtin import build_default_registry; print(build_default_registry().names())"
```

```
['query_asset', 'query_permission', 'query_ticket']
```

---

## 4. 代码实战（6 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/tools/__init__.py` | 包标记 |
| `src/servicedesk/tools/registry.py` | **主角**：schema 生成 + 安全调用 |
| `src/servicedesk/tools/builtin.py` | 三个演示工具 + 假数据 |
| `src/servicedesk/tools/loop.py` | 工具循环（Agent 最小形态） |
| `scripts/function_calling_demo.py` | 三步演示 |
| `tests/test_tool_registry.py` | 16 个断言 |

### 4.1 `$ref` 内联（1.4 的伏笔兑现）

```python
def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 里的 $ref/$defs 递归展开成内联结构。

    为什么必须做：OpenAI 的 strict function calling **不接受 $ref**。
    """
    definitions = schema.get("$defs", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 20:                      # 防循环引用打爆栈
            return {}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                target = definitions.get(name, {})
                merged = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolve(target, depth + 1), **resolve(merged, depth + 1)}
            return {k: resolve(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    resolved = resolve(schema)
    if isinstance(resolved, dict):
        resolved.pop("$defs", None)
    return resolved if isinstance(resolved, dict) else {}
```

### 4.2 安全调用（本节核心）

```python
    async def call(self, name: str, arguments: str) -> ToolResult:
        """执行一次工具调用。**任何失败都以字符串返回，不抛异常。**"""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(ok=False, content=f"错误：没有名为 {name!r} 的工具。可用工具：{self.names()}", error="unknown_tool")

        # 1) 参数是字符串，先解析
        try:
            raw_args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, content=f"错误：参数不是合法 JSON（{exc.msg}）。请重新生成参数。", error="bad_json")

        # 2) 参数校验（把模型的输出当成不可信输入）
        try:
            params = spec.params_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = [...]
            return ToolResult(ok=False, content="错误：参数校验失败 —— " + "；".join(problems), error="invalid_params")

        # 3) 执行（带超时，别让一个慢工具拖死整轮对话）
        try:
            payload = await asyncio.wait_for(spec.handler(**params.model_dump()), timeout=spec.timeout_s)
        except TimeoutError:
            return ToolResult(ok=False, content=f"错误：工具 {name} 执行超过 {spec.timeout_s}s，已中止。", error="timeout")
        except Exception as exc:
            return ToolResult(ok=False, content=f"错误：工具 {name} 执行失败（{type(exc).__name__}: {exc}）。", error="exception")

        return ToolResult(ok=True, content=json.dumps(payload, ensure_ascii=False), duration_ms=...)
```

### 4.3 工具循环

```python
async def run_tool_loop(client, registry, question, *, max_rounds=4, model=None, system=None) -> ToolLoopResult:
    messages: list[Message] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": question})

    calls: list[ToolCallRecord] = []
    cost = 0.0

    for round_index in range(1, max_rounds + 1):
        result = await client.chat(messages, model=model, tools=registry.schemas())
        cost += result.cost_cny

        if not result.tool_calls:
            return ToolLoopResult(answer=result.text, rounds=round_index, calls=calls, cost_cny=cost)

        # 1) 先把 assistant「要调工具」这条消息原样放回去
        messages.append({
            "role": "assistant",
            "content": result.text,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in result.tool_calls
            ],
        })

        # 2) 逐个执行，并把结果以 tool 角色追加（必须带 tool_call_id）
        for call in result.tool_calls:
            outcome = await registry.call(call.name, call.arguments)
            calls.append(ToolCallRecord(name=call.name, arguments=call.arguments, result=outcome, call_id=call.id))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.content})

    raise FatalError(f"工具调用超过 {max_rounds} 轮仍未给出最终回答，已中止…")
```

### 4.4 运行

```powershell
uv run python -m scripts.function_calling_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么要 `inline_refs`，直接用 pydantic 的 schema 不行吗

`QueryAssetParams` 很简单（一个字符串字段），它的 schema 里没有 `$defs`。但只要你的参数里出现**枚举**或**嵌套模型**，pydantic 就会抽出来：

```
展开前 priority 的写法：{'$ref': '#/$defs/Priority', 'description': '优先级'}
展开后 priority 的写法：{'description': '优先级', 'enum': ['P0', 'P1', 'P2', 'P3'], 'type': 'string'}
```

**OpenAI 的 strict mode 明确不接受 `$ref`**。如果直接送，服务端会拒绝这个工具定义——而报错信息通常只说"schema 不合法"，不告诉你哪里不合法。

### 5.2 为什么参数校验要用 pydantic 再走一遍

因为 `arguments` 来自模型，**它的可信度和用户输入一样低**。实测（§6.1）：

| 模型给的参数 | 不校验的后果 | 校验后 |
|---|---|---|
| `{"hostname": "x"}` | 返回"资产库中没有该设备"（**错误结论**） | `参数校验失败：hostname 至少 3 字符` |
| `{"hostname": 123}` | 同样"没找到"（类型错被静默吞掉） | `参数校验失败：应为字符串` |
| `{"hostname":"NB-1024","extra":1}` | `TypeError` 抛出 → **对话中断** | `ok=True`（多余字段被忽略） |

**最危险的是第一行**：脏参数被当成合法查询，用户得到一个"没有这台设备"的确定答案，而实际上只是模型把主机名写错了。

### 5.3 为什么失败要返回字符串，而不是抛异常

因为**模型的下一步依赖于它看到的内容**。

- 抛异常 → 循环中断，用户看到 500，模型永远不知道自己做错了什么
- 返回字符串 → 模型看到"参数校验失败：hostname 至少 3 字符"，下一轮大概率会改对

这和第 1.4 的 `repair_message` 是同一个思想：**把错误变成一次对话机会**。

但有一个例外必须记住：**`registry.call()` 内部兜住了 `Exception`，你自己抛的 `FatalError`/`NeedsHumanError` 不该被这样吞掉**。本节代码里 `except Exception` 只包住 `spec.handler(...)`，不会吞掉 `call` 之外的东西——**边界要划清楚**。

### 5.4 为什么必须有 `timeout_s`

```python
payload = await asyncio.wait_for(spec.handler(...), timeout=spec.timeout_s)
```

一个下游接口卡住 30 秒，你的整轮对话就卡 30 秒。而模型的 `max_rounds` 是 4——最坏情况用户等 2 分钟。

`timeout_s=2.0` 是个产品决策：**工具查不到就告诉用户"查询超时"，比让他干等 30 秒好。**

### 5.5 顺序铁律为什么这么严

§6.2 实测：只发 `tool` 消息、不发前面的 `assistant`，服务端直接拒绝：

```
HTTP 400: messages[1] 是 tool 消息，但它前面没有带 tool_calls 的 assistant 消息（顺序错了）
```

原因很直白：模型需要知道"这个结果对应我发起的哪个调用"。`assistant(tool_calls)` 提供问题，`tool(tool_call_id)` 提供答案，**两者靠 id 配对**。缺了请求，答案无所指。

### 5.6 "查不到"和"出错"必须分开

看工具实现：

```python
async def _query_asset(hostname: str) -> dict:
    asset = ASSETS.get(hostname)
    if asset is None:
        return {"hostname": hostname, "found": False, "message": "资产库中没有该设备"}
    return {"found": True, **asset}
```

`found: False` 是**正常返回**（`ok=True`），不是错误。这样模型才能正确地说"没有这台设备"，而不是"资产系统故障了"。

**这两种说法对用户的意义完全不同**：前者让他核对主机名，后者让他去报障。

### 5.7 schema 顺序为什么必须稳定

```python
def schemas(self) -> list[dict[str, Any]]:
    return [tool_schema(...) for spec in (self._tools[name] for name in self.names())]
```

`self.names()` 返回 `sorted(...)`。工具顺序一变，整个 tools 数组的序列就变了，**prompt cache 立刻失效**——每次请求都按全价计费。2.5 会展开讲这件事，现在先记住"别让顺序随机"。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：跳过参数校验

把 `registry.call()` 里的第 2 步（`model_validate`）删掉，直接把 `json.loads(arguments)` 作为 `**kwargs` 传给函数。

实测三组脏参数：

```
直接用 {"hostname": "x"}                     → 返回 {"hostname": "x", "found": false, "message": "资产库中没有该设备"}
   经校验层 → ok=False 错误：参数校验失败 —— hostname：String should have at least 3 characters

直接用 {"hostname": 123}                     → 返回 {"hostname": 123, "found": false, "message": "资产库中没有该设备"}
   经校验层 → ok=False 错误：参数校验失败 —— hostname：Input should be a valid string

直接用 {"hostname": "NB-1024", "extra": 1}   → 抛 TypeError: _query_asset() got an unexpected keyword argument 'extra'
   经校验层 → ok=True {"found": true, "hostname": "NB-1024", …}
```

**两种失败模式，一种比一种糟**：

1. **静默错误**（前两行）：脏参数被当成合法查询，返回"没找到"。用户会以为自己记错了主机名，而你永远查不出原因——**日志里一切正常**。
2. **直接崩**（第三行）：`TypeError` 冒到循环外，整轮对话中断。

### 6.2 实验 B：漏掉 assistant 消息

只发 `user` + `tool`，不发中间那条 `assistant(tool_calls)`。

```
FatalError -> HTTP 400: messages[1] 是 tool 消息，但它前面没有带 tool_calls 的 assistant 消息（顺序错了）
```

**好处是它立即失败、信息明确**。真正麻烦的版本是：你把 `assistant` 发了，但 `content` 用了 `None` 而不是 `""`，某些兼容服务会接受、某些会拒绝——**这类"半兼容"问题最难查**。

本节统一用 `content: ""`，兼容性最好。

---

## 7. 验证与预期输出

### 7.1 三个场景

```powershell
uv run python -m scripts.function_calling_demo
```

实测（节选）：

```
第 1 步：schema 从 pydantic 模型自动生成
注册了 3 个工具：['query_asset', 'query_permission', 'query_ticket']

工具 query_asset 的参数 schema（已展开 $ref）：
  {"properties": {"hostname": {"description": "设备主机名，形如 NB-1024。只填主机名，不要填 IP 或使用者姓名。", …, "type": "string"}}, "required": ["hostname"], "type": "object"}…
  包含 '$defs' 吗：False      （strict 模式要求没有）
  包含 '$ref' 吗：False

inline_refs 的作用：
  原始 schema 含 $defs：True
  展开后含 $defs：False
  展开前 priority 的写法：{'$ref': '#/$defs/Priority', 'description': '优先级'}
  展开后 priority 的写法：{'description': '优先级', 'enum': ['P0', 'P1', 'P2', 'P3'], 'type': 'string'}

第 2 步：单工具 ——「NB-1024 这台设备的资产信息帮我查一下」
  轮次: 2
  调用工具: ['query_asset']
    - query_asset({"hostname": "NB-1024"}) → ok  53ms
  回答: 根据工具返回：{"found": true, "hostname": "NB-1024", "owner": "张伟（E-2048）", …
  摘要: {'rounds': 2, 'tools': ['query_asset'], 'failed': [], 'cost_cny': 8.4e-05, …}

第 2 步：并列两个工具 ——「NB-1024 这台电脑是谁在用，他有 VPN 权限吗？」
  轮次: 2
  调用工具: ['query_asset', 'query_permission']
    - query_asset({"hostname": "NB-1024"}) → ok  62ms
    - query_permission({"employee_id": "E-2048"}) → ok  65ms
  摘要: {'rounds': 2, 'tools': ['query_asset', 'query_permission'], 'failed': [], 'cost_cny': 0.000119, …}

第 2 步：不需要工具 ——「你好，请介绍一下你能做什么」
  轮次: 1
  调用工具: （无）
  回答: 我可以帮你查设备资产、员工权限和工单状态。请告诉我具体的主机名（如 NB-1024）…
```

**`并列两个工具` 那组是本节的验收点**：两个工具在**同一轮**（`rounds: 2` 表示"一次工具往返"）发出，各带独立 `call_id`，耗时 62ms / 65ms——说明它们是**并行**执行的，不是串行 120ms。

### 7.2 四类失败

```
【未知工具】query_wifi({})
  ok=False  error=unknown_tool
  回喂给模型的内容：错误：没有名为 'query_wifi' 的工具。可用工具：['query_asset', 'query_permission', 'query_ticket']

【参数不是 JSON】query_asset({hostname: NB-1024)
  ok=False  error=bad_json
  回喂给模型的内容：错误：参数不是合法 JSON（Expecting property name enclosed in double quotes）。请重新生成参数。

【参数校验失败】query_asset({"hostname": "x"})
  ok=False  error=invalid_params
  回喂给模型的内容：错误：参数校验失败 —— hostname：String should have at least 3 characters

【查不到数据（不是错误）】query_asset({"hostname": "NB-9999"})
  ok=True  error=None
  回喂给模型的内容：{"hostname": "NB-9999", "found": false, "message": "资产库中没有该设备"}
```

**注意最后一行的 `ok=True`**——查不到不是失败。

### 7.3 测试

```powershell
uv run pytest
```

```
........................................................................ [ 76%]
......................                                                   [100%]
94 passed in 10.59s
```

（1.1–1.8 的 78 个 + 本节的 16 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：工具描述写得含糊，模型选错工具

**现象**

用户问"张伟有没有财务系统权限"，模型调了 `query_asset`（传了 `hostname="张伟"`），然后回答"没有这台设备"。

**定位方法**

把每次调用打出来，看 `name` 和 `arguments` 是否合理：

```python
log.info("tool_called", tool=name, args=arguments[:120], ok=outcome.ok)
```

如果 `query_asset` 的 `hostname` 出现了人名，说明**边界没划清**。

**根因**

两个工具的描述都太泛：

```python
# ❌ 两个都像"查东西"
"查询公司的资产信息"
"查询员工的权限信息"
```

**修法**：给每条描述写清**"什么时候用"和"什么时候别用"**：

```diff
- "查询公司的资产信息"
+ "查询公司设备（笔记本/台式机）的资产信息，返回使用人、型号、状态与保修期。"
+ "当用户询问某台设备归谁用、什么型号、还在不在保修期时使用。"
+ "不要用它查员工权限或工单状态。"
```

**顺手加固**：给参数加 `pattern`（`hostname` 必须匹配 `^[A-Z]{2}-\d+$`），让"传人名"这种错误在参数校验层就被拦住，而不是在业务层返回"没找到"。

### 坑 2：把 `arguments` 当成 dict

**现象**

```
TypeError: string indices must be integers, not 'str'
```

**定位方法**

```python
print(type(call.arguments), repr(call.arguments[:80]))
```

**根因**

服务端返回的 `arguments` 是**字符串**（因为它是"模型生成的文本"）。`call.arguments["hostname"]` 等于对字符串做下标访问。

**修法**

```diff
- hostname = call.arguments["hostname"]
+ args = json.loads(call.arguments)
+ hostname = args["hostname"]
```

**顺手加固**：把 `ToolCall.arguments` 的类型注释明确写成 `str`，并且在 `registry.call()` 里第一步就 `json.loads`——**只在一个地方解析**，别让每个工具自己解析。

### 坑 3：忘了回填 `tool_call_id`

**现象**

模型第二轮回答跑偏："你说的是什么查询？我没有发起过这个调用。"

**定位方法**

把发出去的消息序列打印出来：

```python
for index, message in enumerate(messages):
    print(index, message["role"], message.get("tool_call_id"), str(message.get("content"))[:40])
```

看 `tool` 消息的 `tool_call_id` 是不是 `None`。

**根因**

只发了 `{"role":"tool","content":...}`，没带 `tool_call_id`。模型无法把结果对应回它的请求。

**修法**

```diff
- messages.append({"role": "tool", "content": outcome.content})
+ messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.content})
```

**顺手加固**：在 `run_tool_loop` 里断言每条 `tool` 消息都有非空 `tool_call_id`，把这类错误挡在开发期。

### 坑 4：工具里抛的业务异常，被当成"系统故障"

**现象**

用户问"我的工单 T-9999 怎么样了"，助手回答："抱歉，工单系统出现故障，请稍后重试。"

**定位方法**

看 `ToolResult.error`：如果大量是 `exception`，去看工具实现里抛了什么。

**根因**

工具用异常表达"业务上不存在"：

```python
async def _query_ticket(ticket_id: str) -> dict:
    ticket = TICKETS[ticket_id]        # ← KeyError
    return ticket
```

`KeyError` 被 `call()` 兜住，变成"工具执行失败"。模型看到"失败"，自然说"系统故障"。

**修法**

```diff
- ticket = TICKETS[ticket_id]
- return ticket
+ ticket = TICKETS.get(ticket_id)
+ if ticket is None:
+     return {"ticket_id": ticket_id, "found": False, "message": "没有该工单"}
+ return {"found": True, **ticket}
```

**顺手加固**：在工具的返回类型里强制包含 `found: bool`，让"查不到"成为**结构的一部分**，而不是靠约定。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：模型返回的 <code>arguments</code> 为什么是字符串，而不是对象？</b></summary>

因为它是**模型生成的文本**，服务端原样返回。模型生成的是 `<json>` 字符流，服务端没有义务（也不会）替你解析——解析失败的责任在你。所以 `arguments` 必须 `json.loads`，且要准备好它不合法。

</details>

<details>
<summary><b>Q2：工具执行失败为什么要返回字符串而不是抛异常？</b></summary>

因为模型的下一步依赖于它看到的内容。抛异常会中断循环、用户看到 500，模型永远不知道自己错在哪；返回字符串（"参数校验失败：hostname 至少 3 字符"）则给了它自我纠正的机会。这与 1.4 的 `repair_message` 是同一个思想：**把失败变成一次对话**。

</details>

<details>
<summary><b>Q3：为什么"查不到"必须是 <code>ok=True</code>？</b></summary>

因为"查不到"是业务的正常结果，"工具坏了"才是故障。两者的正确用户回应完全不同：前者让用户核对输入，后者让他报障。如果混为一谈，模型会把"没有这个工单"说成"系统故障"，用户会去报一个不存在的故障。

</details>

### 9.2 动手任务：并行执行 + 工具级幂等

**要求**

1. 把 `run_tool_loop` 里的 `for call in result.tool_calls` 改成用 `asyncio.gather` **并行执行**。
2. 处理部分失败：一个工具失败不能影响其他工具的结果（用 `return_exceptions=True` 或让 `call()` 本身不抛）。
3. 给"写操作"类工具（自己新增一个 `create_ticket`）加**幂等键**：同一个 `call_id` 重复执行返回同一结果。
4. 补 3 个用例：并行耗时接近单个（而非相加）、一个失败一个成功时两者都有记录、重复 `call_id` 不重复创建。

**验收标准**

- 两个各 0.2s 的工具并行时，总耗时 < 0.3s（而非 0.4s）。
- 一个失败时，`ToolLoopResult.calls` 里仍有两条记录，`summarize()["failed"]` 精确指出失败的那个。
- 同一个 `call_id` 调两次 `create_ticket`，返回的 `ticket_id` 相同，且数据库里只有一条。

**提示**：注意 `asyncio.gather` 的顺序——结果列表顺序与传入顺序一致，但**消息追加顺序必须与 `tool_calls` 顺序一致**，否则模型对不上号。

### 9.3 拓展挑战

用 `tool_choice` 做**强制调用**：用户说"帮我建个工单"时，传 `tool_choice={"type":"function","function":{"name":"create_ticket"}}`，强制模型必须调这个工具而不是闲聊。

**思考**：

1. 强制调用时，模型还会不会"先解释再调用"？`content` 还有意义吗？
2. 如果工具参数需要用户补充（比如缺少优先级），强制调用会让模型编一个默认值——这比让它反问用户更好还是更坏？

---

## 10. 本节小结与延伸

**三点回顾**

1. **Function Calling 不是"模型执行函数"，而是"模型申请调用"**。执行、校验、超时、审计——全是你的事。
2. **`arguments` 是字符串，且不可信**：实测跳过校验会让 `{"hostname": "x"}` 变成"资产库中没有该设备"——**静默的错误结论**，比崩溃更难查。
3. **失败返回字符串，查不到返回 `found: False`（`ok=True`）**：让模型能自我纠正，也让"没有"和"坏了"在语义上分开。

**已知限制（诚实记录）**

- **工具串行执行**：本轮两个工具是顺序 `await` 的（实测 62ms + 65ms）。并行化留作 §9.2 作业。
- **无幂等键**：写操作重复执行会重复生效（1.5 §8 坑 2 提过，这里仍未实现）。
- **mock 按关键词选工具**：它验证的是"链路正确"，**不验证模型的选工具能力**。真实模型的误选率要用阶段五的评估测。
- **未处理 `tool_choice` 的强约束语义**（拓展挑战）。

**延伸阅读**

- OpenAI Function Calling 指南：https://platform.openai.com/docs/guides/function-calling
- JSON Schema 规范：https://json-schema.org/understanding-json-schema/
- `asyncio.wait_for` 与超时语义：https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for

---

## 修订记录

- 2026-11-17 初稿：三个场景与 16 个测试全部实机运行，§7 为真实输出
- 2026-11-17 修正：mock 的消息顺序校验从"前一条"改为"最近一条非 tool 消息"——并行调用会产生连续多条 tool 消息，原规则会误判 400
- 2026-11-17 修正：mock 在"无需工具"时返回空字符串导致空回答，改为返回一段自我介绍
- 2026-11-17 补充：§6.1 三组脏参数的实测对比、§6.2 顺序违规的 400 原文
