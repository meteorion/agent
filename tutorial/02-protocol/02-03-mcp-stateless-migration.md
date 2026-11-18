---
section: "2.3"
title: "2026-07-28 无状态改造：握手与 Session 去了哪"
stage: 2
status: done
depth: L2
difficulty: 4
minutes: 90
milestone: M2
depends_on: ["2.2"]
produces: ["docs/mcp-stateless.md"]
verifies: "能对着时序图讲出「为什么这版可以在 Serverless 上冷启动」"
part: null
updated: "2026-11-17"
---

# 2.3 2026-07-28 无状态改造：握手与 Session 去了哪

> 本节依据官方 [Key Changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、
> [Versioning 页](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)、
> [发布 blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/) 编写。
> 标注 ⚠️ 的地方是"正文未逐字核实"，落地前请对照 [schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)。

## 0. 本节导航

**本节在链路中的位置**：阶段二的技术核心。2.4 要写的 Server、2.5 的 Streamable HTTP、2.6 的 MRTR + OAuth，全都建立在"无状态"这一个前提上。**这一节没吃透，后面每一行代码都会写得心里没底。**

**本节你将获得**

1. 两张时序图：旧版握手 vs 新版逐请求自描述，能一眼看出差别在哪。
2. 一份完整的"删掉了什么 → 替代方案"对照表（12 项）。
3. **状态去哪了**的三个答案，以及为什么"显式句柄"比"隐藏 session"好。
4. 可运行的信封模块：`_meta` 构造、版本协商、`resultType` 容错、MRTR 重试、句柄存储。
5. 两个真实风险：把"缺 `resultType`"当错误会让旧服务全灭；句柄用自增 id 会被枚举。

**前置检查清单**

- 2.2 的 `uv run python -m scripts.mcp_decision_demo` 能跑通。
- 知道 MCP 的 Host / Client / Server 三角色（2.2 §2.3）。

**难度**：★★★★☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（纯本地，不调模型、不连网络）

---

## 1. 为什么需要它（痛点先行）

先看一个运维场景。

你的 MCP Server 跑在 3 台容器上，前面是普通轮询负载均衡。用户第一次调用了"查工单"，
返回"工单 T-1001 处理中"，**同时服务端在 session 里记了一笔**："这个用户正在处理 T-1001"。

第二个请求问"那能帮我催一下吗？"——落到**另一台实例**。那台上没有这个 session，于是：

```
404: Session not found
```

你开始排查。三个选择，每个都很难受：

1. **粘性路由**（同一个客户端固定打同一台）→ 一台挂了，用户全部掉线；扩容时老用户仍然卡在旧实例
2. **共享 session 存储**（Redis）→ 每次请求多一次网络往返，而且 Redis 挂了你全挂
3. **改代码，别用 session** → 这才是对的，但要改协议

**官方的选择是第 3 个，而且是在协议层改。** 2026-07-28 把 `initialize` 握手和
`Mcp-Session-Id` 全部删掉，让**每个请求自带全部上下文**。

结果是：任意请求可以落到任意实例，负载均衡用最普通的轮询就行，Serverless 冷启动天然成立。

---

## 2. 核心概念与架构设计

### 2.1 类比：酒店入住 vs 每次出示身份证

- **旧版（session）= 酒店入住**：你在前台登记一次，拿到房卡（`Mcp-Session-Id`），之后刷卡进房。
  房卡只在这一家店、这一次住宿有效。换一家店（另一台实例），卡就失效了。
- **新版（逐请求元数据）= 每次出示身份证**：你每次进任何一家店，都出示证件说明"我是谁、我要什么"。
  任何前台都能受理，不需要"先登记"。

> **这个类比在哪失效**：身份证是**长期有效且全局唯一**的，而协议版本会变。
> 所以新版要求每次请求都声明"我说的是哪一版协议"——**同一张身份证，配上不同的办事规则**。
> 版本不匹配时，前台必须明确告诉你"我这里只办这几版"，而不是猜。

### 2.2 旧版时序：先握手，再干活

```
Client                                    Server
  │ ① initialize (protocolVersion, caps)     │
  │ ────────────────────────────────────────►│
  │ ② InitializeResult                       │
  │ ◄────────────────────────────────────────│  ← 建立 session
  │    (Mcp-Session-Id: 7f3a…)               │
  │ ③ notifications/initialized              │
  │ ────────────────────────────────────────►│
  │ ④ tools/list   + Mcp-Session-Id          │
  │ ⑤ tools/call   + Mcp-Session-Id          │
```

**约束**：④⑤ 必须打到同一台实例。这就是"粘性路由 / 共享存储"的全部来源。

### 2.3 新版时序：每个请求自描述

```
Client                                    Server（任意实例）
  │ ① server/discover（可选）                 │
  │ ────────────────────────────────────────►│
  │ ② DiscoverResult(versions, caps, identity)│
  │ ◄────────────────────────────────────────│
  │ ③ tools/call                             │
  │    _meta: {protocolVersion, clientInfo,  │
  │            clientCapabilities}           │
  │    headers: MCP-Protocol-Version,        │
  │             Mcp-Method, Mcp-Name         │
  │ ────────────────────────────────────────►│
  │ ④ resultType: "complete"                 │
  │ ◄────────────────────────────────────────│
```

真实请求体（官方 blog 原文）：

```http
POST /mcp HTTP/1.1
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: search

{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"search","arguments":{"q":"otters"},
 "_meta":{"io.modelcontextprotocol/clientInfo":{"name":"my-app","version":"1.0"}}}}
```

**两个细节值得注意**：

- `_meta` 是 `params` 的**成员**，不是顶层字段。
- HTTP 上协议版本**同时**出现在 `_meta` 与 `MCP-Protocol-Version` 头里——头是给网关看的，`_meta` 是给 Server 看的。

### 2.4 状态去哪了（**本节最重要的设计问题**）

删掉 session ≠ 你的应用必须无状态。官方给的答案是：

> 让工具**签发一个显式句柄**，由模型把它当普通参数带回来。
> 这比把状态藏在传输层更好——**模型能看见它、能在工具之间传递、能重放、能调试**。

三种承载方式：

| 方式 | 放在哪 | 优点 | 代价 |
|---|---|---|---|
| **服务端签发句柄**（推荐） | 业务存储（Redis/DB） | 可审计、可过期、模型可见 | 要设计 id 与 TTL |
| 请求内 `_meta` | 每次请求自带 | 完全无状态 | 有大小与敏感信息风险 |
| 会话外置 | 共享存储 + Cookie/Header | 迁移成本最低 | 仍是"隐藏状态"，难调试 |

### 2.5 适用范围（边界）

| 情况 | 建议 |
|---|---|
| 无状态的查询类工具（查资产、查工单） | ✅ 直接迁，工作量最小 |
| 需要多轮确认的写操作（建单、退款） | ✅ 用 MRTR + 句柄 |
| 强依赖服务端主动推送状态 | ⚠️ 改用 `subscriptions/listen`，不要指望 session |
| 依赖 SSE 断线续传 | ❌ 该能力已移除，必须改"重发" |

---

## 3. 环境与版本

本节**不新增依赖**、不连网络。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| Python | `3.12.13` | `uuid` / `time` / `itertools` |
| 协议版本 | `2026-07-28` | `PROTOCOL_VERSION` 常量 |

**验证**

```powershell
uv run python -c "from servicedesk.mcp.envelope import PROTOCOL_VERSION; print(PROTOCOL_VERSION)"
```

```
2026-07-28
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/mcp/envelope.py` | **主角**：信封 / 协商 / 归一化 / MRTR / 句柄 |
| `scripts/mcp_stateless_demo.py` | 七个演示步骤 |
| `tests/test_mcp_envelope.py` | 20 个断言 |
| `docs/mcp-stateless.md` | 交付文档（时序图 + 迁移检查表） |

### 4.1 构造自描述请求

```python
def build_request(method, params=None, *, client_name="servicedesk-agent",
                  client_version="0.1.0", client_capabilities=None,
                  protocol_version=PROTOCOL_VERSION, log_level=None,
                  traceparent=None, request_id=None) -> dict:
    meta: dict[str, Any] = {
        META_PROTOCOL_VERSION: protocol_version,
        META_CLIENT_INFO: {"name": client_name, "version": client_version},
    }
    if client_capabilities is not None:
        meta[META_CLIENT_CAPABILITIES] = client_capabilities
    if log_level is not None:
        # 旧版的 logging/setLevel 被移除了，日志级别改成每请求声明
        meta[META_LOG_LEVEL] = log_level
    if traceparent is not None:
        # W3C Trace Context 通过 _meta 传播
        meta["traceparent"] = traceparent

    body_params = dict(params or {})
    body_params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": ..., "method": method, "params": body_params}
```

### 4.2 HTTP 头

```python
def build_headers(method, *, name=None, protocol_version=PROTOCOL_VERSION, traceparent=None):
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    return headers
```

### 4.3 版本协商

```python
class UnsupportedProtocolVersionError(RuntimeError):
    def __init__(self, requested: str, supported: Iterable[str]) -> None:
        self.requested = requested
        self.supported = sorted(set(supported))
        super().__init__(f"不支持的协议版本 {requested!r}；服务端支持：{self.supported}")


def check_protocol_version(requested: str, supported: Iterable[str]) -> str:
    supported_list = list(supported)
    if requested not in supported_list:
        raise UnsupportedProtocolVersionError(requested, supported_list)
    return requested
```

### 4.4 `resultType` 归一化（兼容旧 Server 的唯一理由）

```python
def result_type(response: dict[str, Any]) -> str:
    result = response.get("result")
    if not isinstance(result, dict):
        return RESULT_COMPLETE
    value = result.get("resultType")
    return value if value in VALID_RESULT_TYPES else RESULT_COMPLETE
```

### 4.5 MRTR：重试原请求

```python
def build_retry_with_inputs(original_request, responses) -> dict:
    retried = {
        "jsonrpc": "2.0",
        "id": original_request["id"],
        "method": original_request["method"],
        "params": dict(original_request["params"]),
    }
    retried["params"]["inputResponses"] = responses
    return retried
```

### 4.6 显式句柄

```python
class HandleStore:
    def mint(self, data: dict[str, Any]) -> str:
        handle = f"h_{uuid.uuid4().hex[:16]}"      # 不可猜测
        self._records[handle] = HandleRecord(dict(data), expires_at=now + self._ttl_s, ...)
        return handle

    def resolve(self, handle: str) -> dict[str, Any] | None:
        record = self._records.get(handle)
        if record is None or record.expires_at < time.time():
            self._records.pop(handle, None)
            return None
        return dict(record.data)
```

完整实现见 [src/servicedesk/mcp/envelope.py](../../servicedesk-agent/src/servicedesk/mcp/envelope.py)。

### 4.7 运行

```powershell
uv run python -m scripts.mcp_stateless_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 `_meta` 要带业务前缀

```python
META_PREFIX = "io.modelcontextprotocol/"
META_PROTOCOL_VERSION = META_PREFIX + "protocolVersion"
```

因为 `params` 里已经有业务字段（`name`、`arguments`），如果不加前缀，某天某个工具的参数
就叫 `protocolVersion`，你就撞车了。**带前缀是给未来的自己留退路。**

### 5.2 为什么版本要"逐请求判断"而不是"协商一次"

协商一次的隐含前提是"我们之间有一条持续的连接/会话"。**现在没有会话了**，所以每一次请求都必须
重新声明，服务端逐请求判断。这带来一个额外好处：**可以灰度**——同一台 Server 同时接受
`2026-07-28` 和 `2025-11-25`，按请求分流。

### 5.3 `UnsupportedProtocolVersionError` 为什么要列出支持版本

因为客户端需要**可执行的下一步**。只回一句"不支持"，客户端只能瞎猜或放弃；
给出 `supported=[...]`，客户端就能选一个自己支持的版本重试，或退到 legacy 通道。

这和 1.5 的三层异常是同一个思想：**错误信息要能驱动决策。**

### 5.4 `resultType` 缺失为什么必须按 `complete` 处理

规范原文要求：客户端 **MUST** 把缺少该字段的旧版结果当作 `"complete"`。

原因很实际：2025-11-25 及更早的 Server 不知道这个字段，**它们永远不会带**。
如果你把"缺失"当异常，升级当天所有旧服务都会失效。

§6.1 实测：10 个旧版响应，严格逻辑通过 **0 个**，容错逻辑通过 **10 个**。

### 5.5 MRTR 为什么是"重试原请求"而不是"发新请求"

因为**服务端要能无状态**。

如果客户端发一个新请求（新 id），服务端就必须记住"上一轮我问了什么、对应哪个调用"——
这就是 session，白改了。

而"重试原请求 + `inputResponses`"让服务端在收到请求的那一刻，**同时拥有**原始参数和补充答案，
不需要任何记忆。

### 5.6 句柄为什么不能用自增 id

因为句柄是**客户端可见、模型可传**的。自增 id 意味着可以枚举。

§6.2 实测：`h_1`/`h_2`/`h_3` 三个句柄，攻击者从 `h_1` 试到 `h_3`，**读到了全部三个用户的记录**
（含 `salary` 字段）。而 `uuid4` 生成的 `h_a465017b4d414dbb` 猜 `h_1` 直接返回 `None`。

**这是本节最容易被忽略的安全点**：session 是"服务端藏的"，句柄是"客户端可见的"，
**可见的东西必须不可猜测。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把"缺 `resultType`"当成错误

```python
# 严格版（错误示范）
if response["result"].get("resultType") != "complete":
    raise ProtocolError("missing resultType")
```

实测 10 个旧版响应（`{"result": {"content": []}}`）：

```
10 个旧版响应：严格逻辑通过 0 个，容错逻辑通过 10 个
```

**升级当天，所有 2025-11-25 的 Server 全部失效。** 而且报错是"missing resultType"——
你的运维会以为是网络或版本问题，不会想到是自己这一行判断。

> 修法就是规范写的那句：**缺失或不认识，一律按 `complete`**。

### 6.2 实验 B：句柄用自增 id

```python
# 错误示范
self.n += 1
handle = f"h_{self.n}"
```

实测：

```
三个用户拿到句柄：['h_1', 'h_2', 'h_3']
攻击者从 h_1 试到 h_3：
  h_1 -> {'user': '用户1', 'salary': 10001}
  h_2 -> {'user': '用户2', 'salary': 10002}
  h_3 -> {'user': '用户3', 'salary': 10003}
```

**横向越权，一行代码。**

而且这个 bug 在功能测试里**完全看不出来**：你测"用户 1 能不能读自己"，通过；你测"用户 2 能不能读自己"，也通过。**只有主动去猜别人的 id 才会暴露。**

修法：`uuid4`（实测 `h_a465017b4d414dbb`，猜 `h_1` 返回 `None`），并且**句柄要绑定用户**
（`resolve` 时校验 `handle.owner == 当前用户`，本节未实现，见 §9.2）。

---

## 7. 验证与预期输出

### 7.1 七个演示步骤

```powershell
uv run python -m scripts.mcp_stateless_demo
```

实测（节选）：

```
第 2 步：一个自描述的请求长什么样
  请求体：
    {
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
        "name": "query_asset",
        "arguments": {"hostname": "NB-1024"},
        "_meta": {
          "io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "servicedesk-agent", "version": "0.1.0"},
          "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
          "io.modelcontextprotocol/logLevel": "info",
          "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        }
      }
    }

  HTTP 头：
    MCP-Protocol-Version: 2026-07-28
    Mcp-Method: tools/call
    Mcp-Name: query_asset
    traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01

第 3 步：版本不匹配会怎样
  服务端支持：['2026-07-28']
  客户端声明 '2026-07-28'：✅ 接受
  客户端声明 '2025-11-25'：❌ 拒绝
       错误信息：不支持的协议版本 '2025-11-25'；服务端支持：['2026-07-28']

第 5 步：resultType 的向后兼容
  新版普通结果                  → resultType='complete'      input_required=False
  新版要补充输入                → resultType='input_required' input_required=True
  旧版 Server（没有该字段）      → resultType='complete'      input_required=False
  奇怪的值                     → resultType='complete'      input_required=False
  错误响应（没有 result）        → resultType='complete'      input_required=False

第 6 步：MRTR
  服务端返回 resultType=input_required，需要补充：
    - [prio] 这条工单的优先级是？
    - [approve] P1 需要主管审批，是否已审批？
  客户端**重试原请求**，把答案放进 inputResponses

第 7 步：为什么这版可以 Serverless 冷启动
  【做法 A：状态藏在实例内存里（旧版思路）】
    请求 1 → i-1：建立 session，记录 user=张伟
    请求 2 → i-2：查内存 → 找不到 session ❌
  【做法 B：状态放在显式句柄里（新版思路）】
    请求 1 → i-1：工具返回 handle=h_80d4d5159207423d
    请求 2 → i-2：模型把 handle 当参数传回 → {'user': '张伟', 'step': 1} ✅
    请求 2 → i-3：模型把 handle 当参数传回 → {'user': '张伟', 'step': 1} ✅
```

### 7.2 测试

```powershell
uv run pytest
```

```
........................................................................ [ 57%]
.....................................................                    [100%]
125 passed in 10.57s
```

（1.x 的 78 + 2.1 的 16 + 2.2 的 11 + 本节的 20。）

### 7.3 一句话回答"为什么能 Serverless 冷启动"

> **因为状态不在实例里，而在请求里与句柄里。** 冷实例收到第一个请求时，
> 它需要的版本、身份、能力、参数全都在这个请求里；它不需要"记得"任何上一轮的事，
> 所以不需要预热、不需要粘性路由、不需要共享会话存储。

---

## 8. 生产环境踩坑实录

### 坑 1：升级后老客户端全部报"版本不支持"

**现象**

Server 升级到 2026-07-28 后，一批 2025-11-25 的客户端全部失败，报 `UnsupportedProtocolVersionError`。

**定位方法**

```python
log.warning("unsupported_version", requested=requested, supported=supported, peer=ip)
```

看 `requested` 是不是集中在某几个旧版本。

**根因**

Server **只**声明支持 2026-07-28。而规范给的弃用窗口是 12 个月——
意味着过渡期内，Server 应该是 **dual-era**：既接受 modern 的逐请求元数据，也保留 `initialize` 路径。

**修法**

```diff
- SUPPORTED = ["2026-07-28"]
+ SUPPORTED = ["2026-07-28", "2025-11-25"]   # 过渡期双栈
```

并在 `server/discover` 里如实广播。

**顺手加固**：按 `requested` 版本打点统计占比。当旧版本占比低于阈值（比如 1%）再摘掉。

### 坑 2：把"重试"实现成"发新请求"，MRTR 直接失效

**现象**

服务端返回 `input_required`，客户端补了答案再发一次，服务端报"缺少必填参数"或"不认识的调用"。

**定位方法**

对比两次请求的 `id` 和方法：

```python
print("first:", first["id"], first["method"])
print("retry:", retry["id"], retry["method"])
```

如果 `id` 变了，就是这个坑。

**根因**

MRTR 的语义是**重试原请求**（同 id、同方法、同参数），只多一个 `inputResponses`。
发新请求意味着服务端要记住上一轮问过什么——那又回到了 session。

**修法**

```diff
- retry = build_request("tools/call", new_params)      # ❌ 新请求
+ retry = build_retry_with_inputs(original, answers)   # ✅ 重试原请求
```

**顺手加固**：在 `build_retry_with_inputs` 里断言 `id` 与原请求一致，把这类错误挡在开发期。

### 坑 3：以为"断流重连"还能续上

**现象**

流式响应中途网络抖动，客户端断线重连后，拿到的结果**缺了一段**，而且没人报错。

**根因**

2026-07-28 **移除了 SSE 断线续传**：没有 `Last-Event-ID`，没有事件 ID。
流断了就是**请求丢了**。

**修法**

```diff
- # 旧版：重连带 Last-Event-ID 续传
- headers["Last-Event-ID"] = last_event_id
+ # 新版：用新的 request id 重发整个请求
+ retry = build_request(method, params)   # 全新 id
```

**顺手加固**：把"流中断"当成一次**失败**来处理（1.5 的 `RetryableError`），
而不是当成"可以从中间继续"。对用户要明确说"这次查询失败了，正在重试"。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：没有 session 了，多轮工具调用怎么做？</b></summary>

靠两样东西：**`messages` 里的对话历史**（谁说了什么），以及**服务端签发的显式句柄**（跨调用的业务状态）。前者是模型侧的上下文，后者是业务侧的存储。两者都不依赖传输层的会话。

</details>

<details>
<summary><b>Q2：为什么"缺 resultType 按 complete"是必须的，而不是可选的好习惯？</b></summary>

因为旧版 Server（2025-11-25 及更早）不会带这个字段，而它们会长期存在（12 个月弃用窗口）。把"缺失"当错误，等于升级当天让所有旧服务失效。实测：10 个旧版响应，严格逻辑通过 0 个。

</details>

<details>
<summary><b>Q3：MRTR 为什么能不加状态？</b></summary>

因为它要求客户端**重试原请求**（同 id、同方法、同参数），只是多带 `inputResponses`。服务端收到时，原始参数和补充答案在同一个请求里，不需要记忆"上一轮问过什么"。如果客户端发新请求，服务端就必须记住上下文——那又回到 session 了。

</details>

### 9.2 动手任务：给句柄加上"归属校验"

**要求**

1. `HandleStore.mint(data, *, owner_id)` 记录句柄归属。
2. `HandleStore.resolve(handle, *, requester_id)` 在归属不匹配时返回 `None`，并记一条安全日志。
3. 增加 `revoke(handle)` 主动作废。
4. 补 4 个用例：归属匹配、归属不匹配、主动作废、过期。

**验收标准**

- 用户 A 拿到 `h_x`，用户 B 用 `h_x` 调用 → 返回 `None`，日志里有 `handle_owner_mismatch`。
- `revoke` 之后立即失效。
- 原有 20 个用例仍全绿。

**提示**：归属校验要放在 `resolve` **内部**，不能交给调用方——否则总有人忘记校验（和 1.7 §9.3 的多租户过滤是同一个道理）。

### 9.3 拓展挑战

写一个 **dual-era 版本协商器**：同一台 Server 同时接受 `2026-07-28` 与 `2025-11-25`。

**思考**：

1. 收到 `2025-11-25` 的请求时，`_meta` 里没有 `protocolVersion`（旧版放在握手里），你怎么判断对方是 legacy？
2. 双栈期间，`server/discover` 该返回哪些版本？
3. 什么时候可以摘掉 legacy？用什么数据支撑这个决定？

---

## 10. 本节小结与延伸

**三点回顾**

1. **握手与 session 都进了 `_meta`**：版本、身份、能力逐请求声明，服务端逐请求判断。好处不只是"能扩容"，而是**任意请求可落任意实例**。
2. **状态没有消失，只是显式化了**：服务端签发句柄，模型当参数带回来。实测用自增 id 做句柄，攻击者 `h_1→h_3` 就能读到全部三个用户的数据（含 `salary`）。
3. **`resultType` 缺失必须按 `complete`**：这不是好习惯，是旧服务端还能活的唯一条件。实测严格逻辑通过 0/10。

**已知限制（诚实记录）**

- **`server/discover` 返回体字段名未逐字核实**：代码里做了容错读取（`protocolVersions` / `supportedProtocolVersions` / `versions`），取不到返回空表而不是瞎猜。⚠️ 落地前请对 schema。
- **兼容矩阵是按术语推导的**：`docs/mcp-stateless.md` §4 的表标注了 ⚠️，不是规范原文表格的逐字复制。
- **句柄未做归属校验**：§9.2 是作业。
- **本节未写真实 Server**：只做了信封与协商，真正的 MCP Server 在 2.4。

**延伸阅读**

- Key Changes（本节的一手依据）：https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Versioning 与兼容性：https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- 发布 blog（含真实请求样例与 MRTR 说明）：https://blog.modelcontextprotocol.io/posts/2026-07-28/
- `server/discover`：https://modelcontextprotocol.io/specification/2026-07-28/server/discover

---

## 修订记录

- 2026-11-17 初稿：七个演示步骤与 20 个测试全部实机运行，§7 为真实输出
- 2026-11-17 依据官方 changelog / versioning / blog 校正：`_meta` 键名、HTTP 头（`MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name`）、MRTR 的“重试原请求”语义、`resultType` 缺失按 `complete`、句柄方案的官方表述
- 2026-11-17 标注：`server/discover` 返回体字段名与兼容矩阵为“按语义推导”，已在正文与 `docs/mcp-stateless.md` 中加 ⚠️
