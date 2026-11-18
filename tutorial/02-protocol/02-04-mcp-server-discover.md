---
section: "2.4"
title: "用 server/discover + 三原语写第一个无状态 MCP Server"
stage: 2
status: done
depth: L2
difficulty: 4
minutes: 120
milestone: M2
depends_on: ["2.3"]
produces: ["mcp/server.py"]
verifies: "用 MCP Inspector 连上能看到 discover 结果与 3 个工具"
part: null
updated: "2026-11-17"
---

# 2.4 用 server/discover + 三原语写第一个无状态 MCP Server

> **验收方式说明**：toc 里写的是"用 MCP Inspector 连上"——那是个 GUI 工具，本机跑不了。
> 本节用**等价的 stdio 客户端实测**（真实子进程 + 管道，4 条 JSON-RPC 往返）作为替代验证，
> 并在 §7.3 给出 Inspector 的启动命令，你有环境时可以直接接上。

## 0. 本节导航

**本节在链路中的位置**：2.3 把协议讲清了，本节把它**跑起来**。2.5 要给它换成 Streamable HTTP、2.6 要加 OAuth、2.7 要把它接回 Agent——全都基于本节这个 Server。

**本节你将获得**

1. 一个不依赖任何 MCP SDK 的 Server：手写 JSON-RPC，才看得清协议。
2. `server/discover` 的完整实现（版本 / 能力 / 身份），以及为什么它是"必做项"。
3. 三原语各一个真实实现：3 个 Tools、1 个 Resource、1 个 Prompt。
4. `tools/list` 的**确定性排序 + `ttlMs`/`cacheScope`**，并实测"注册顺序变了、输出不变"。
5. 一条铁律：**stdout 只能写协议，日志必须走 stderr**——这条我在写 demo 时真的踩了。

**前置检查清单**

- 2.3 的 `uv run python -m scripts.mcp_stateless_demo` 能跑通。
- 2.1 的 `ToolRegistry` 可用（本节的 `tools/call` 直接复用它）。

**难度**：★★★★☆ ｜ **预计时长**：120 分钟 ｜ **本节费用**：**¥0**（纯本地，stdio 子进程）

---

## 1. 为什么需要它（痛点先行）

到现在为止，"MCP"一直是个概念。你有信封构造器（2.3）、有工具注册表（2.1），
但**没有一个真的 Server 能让别的进程连上来**。

这就像你造好了锁和钥匙，但还没有门。

**第一个真实的坎**：你以为要装 SDK。装 `mcp` Python 包、看它的 builder API、理解它的装饰器——
然后你依然不知道线上那个 400 是谁返回的。

**本节的选择是：手写。** 因为 MCP 的传输层其实极简——**stdio 上就是"一行一条 JSON-RPC"**。
手写一遍，你会看清三件事：

1. 一个 MCP Server 就是"一个读 stdin、写 stdout 的循环"，没有魔法
2. 错误分两层：**协议层 `error`** 与 **工具层 `isError`**，混了就会误判
3. **日志写错地方，客户端直接崩**——而这是最容易犯的错

---

## 2. 核心概念与架构设计

### 2.1 类比：政务大厅的一个窗口

一个 MCP Server 就像政务大厅里**一个窗口**，窗口上挂四块牌子：

| 牌子 | 对应 | 谁来看 |
|---|---|---|
| **大厅导览牌** | `server/discover` | 任何人进门先看：这里办哪些事、几点上班（版本/能力/身份） |
| **业务清单** | `tools/list` + `tools/call` | 你来办事：能办什么、要带什么材料（schema） |
| **档案架** | `resources/list` + `resources/read` | 你出示证件后可查阅的档案（只读、按编号取） |
| **空白表单** | `prompts/list` + `prompts/get` | 大厅提供的标准表格，你填了再交（模板，不是程序逻辑） |

> **这个类比在哪失效**：窗口不会"忘事"，但**它也不记得你**——同一个窗口，你上午来过、
> 下午再来，它不会主动接上上午的话。**这正是无状态的含义**：每次都要重新出示材料（`_meta`）。

### 2.2 三种原语的实现形态

```
Tools      → 复用 2.1 的 ToolRegistry（有参数校验、超时、错误字符串）
Resources  → 静态列表 + 按 URI 取文本（只读，Host 决定挂载）
Prompts    → 静态模板 + 参数替换（用户选择，不是模型调用）
```

**它们共用同一个 JSON-RPC 分发器**，区别只在返回体的形状。

### 2.3 两层错误（**最容易混的地方**）

```
                    ┌─────────────────────────────────┐
   协议层错误         │ {"error": {"code": -32601, ...}} │  ← 方法不存在、参数缺失、版本不支持
   （JSON-RPC）      └─────────────────────────────────┘
                    ┌─────────────────────────────────┐
   工具层错误         │ {"result": {"isError": true,     │  ← 工具内部失败、参数校验失败
   （业务语义）       │             "content": [...]}}   │     但协议层是**成功响应**
                    └─────────────────────────────────┘
```

**为什么必须分开**：协议层错误意味着"你这次请求本身不合法"，客户端该修代码；
工具层错误意味着"请求合法但执行没成功"，模型该看到错误文本并自我纠正（2.1 §5.3）。

实测（§7）：`query_asset` 传 `hostname: "x"` → **`isError: true`，但响应里没有 `error` 字段**。

### 2.4 stdio 的帧格式

```
客户端 stdin ──► {"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}\n
                 {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n
Server stdout ◄─ {"jsonrpc":"2.0","id":1,"result":{...}}\n
                 {"jsonrpc":"2.0","id":2,"result":{...}}\n
```

**一行一条消息，按行切分。** 没有长度前缀、没有分帧头。

### 2.5 适用范围（边界）

| 传输 | 什么时候用 |
|---|---|
| **stdio** | 本地工具、编辑器插件、单机 Agent（本节） |
| **Streamable HTTP** | 远程服务、多客户端、Serverless（2.5） |
| 旧版 HTTP+SSE | ❌ 已弃用，别再用新项目 |

---

## 3. 环境与版本

本节**不新增依赖**、不连网络。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| Python | `3.12.13` | `asyncio` / `json` |
| pydantic | `2.14.3` | 工具参数 schema（经 2.1） |
| structlog | `26.1.0` | 工具层日志（**要指到 stderr**） |

**验证**

```powershell
uv run python -c "from servicedesk.mcp.server import McpServer; print(McpServer().discover()['serverInfo'])"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/mcp/server.py` | **主角**：Server + 分发 + stdio 循环 |
| `scripts/mcp_server_demo.py` | 七步演示（in-process） |
| `tests/test_mcp_server.py` | 20 个断言 |

### 4.1 `server/discover`（必须实现）

```python
    def discover(self) -> dict[str, Any]:
        """Server **必须**实现：广播支持的版本、能力与身份。"""
        return {
            "protocolVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
```

### 4.2 `tools/list`（确定性排序 + 可缓存）

```python
    def list_tools(self) -> dict[str, Any]:
        """确定性排序 + 缓存提示。顺序一变，prompt cache 就失效。"""
        tools = [
            {
                "name": schema["function"]["name"],
                "description": schema["function"]["description"],
                "inputSchema": schema["function"]["parameters"],
            }
            for schema in self.registry.schemas()
        ]
        tools.sort(key=lambda item: item["name"])
        return {"tools": tools, "ttlMs": 60_000, "cacheScope": "global"}
```

### 4.3 入口：为什么是 `handle_async`

```python
    async def handle_async(self, request: dict[str, Any]) -> dict[str, Any] | None:
        checked = self._precheck(request)
        if isinstance(checked, dict):
            return checked
        request_id, method, params, request_level = checked

        try:
            if method == "tools/call":
                return await self._call_tool(request_id, params, request_level)
            result = self._dispatch(method, params)
        except MethodNotFound as exc:
            return self._error(request_id, METHOD_NOT_FOUND, str(exc))
        except JsonRpcInvalidParams as exc:
            return self._error(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:
            self._log("error", "internal_error", method=method, error=repr(exc))
            return self._error(request_id, INTERNAL_ERROR, f"内部错误：{type(exc).__name__}")

        return {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", **result}}
```

**注意最后一个 `except Exception`**：一个工具崩了不能打崩整个循环（stdio 一旦异常退出，客户端就断线了）。

### 4.4 日志级别来自每个请求

```python
    def log_level_of(self, params: dict[str, Any]) -> str:
        """日志级别来自**每个请求**的 `_meta`，不是连接级设置。"""
        meta = params.get("_meta") or {}
        value = str(meta.get(META_LOG_LEVEL) or DEFAULT_LOG_LEVEL).lower()
        return value if value in LOG_LEVELS else DEFAULT_LOG_LEVEL
```

### 4.5 stdio 循环

```python
async def serve_stdio(server=None, stdin=None, stdout=None) -> None:
    server = server or McpServer()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = McpServer._error(None, PARSE_ERROR, f"JSON 解析失败：{exc.msg}")
        else:
            response = await server.handle_async(request)

        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
```

完整实现见 [src/servicedesk/mcp/server.py](../../servicedesk-agent/src/servicedesk/mcp/server.py)。

### 4.6 运行

```powershell
uv run python -m scripts.mcp_server_demo
uv run pytest

# 真实 stdio 往返（把请求喂给独立进程）
'{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}' |
    uv run python -m servicedesk.mcp.server
```

---

## 5. 逐块拆解

### 5.1 为什么 `handle_async` 而不是 `handle`

因为 `tools/call` 要 `await` 工具执行（2.1 的 `registry.call` 是协程）。
如果强行做成同步的 `handle()`，你就得在里面 `asyncio.run()` 一次——那会在已有事件循环里直接崩。

**约定：整个分发是协程，纯同步的部分在内部用普通函数。** 这样 `serve_stdio` 也是协程，能跑在 uvloop / AnyIO 里。

### 5.2 `ttlMs` 与 `cacheScope` 为什么必须给

规范要求列表类响应带这两个字段（2.3 §2 的变更表）。原因：

- `ttlMs` 告诉客户端"这份清单 60 秒内不用再问"
- `cacheScope: "global"` 告诉它"这份清单对所有租户一样，可以共享缓存"

没有它们，客户端只能每次重连都重拉一遍 `tools/list`。**工具目录一多，这一拉就是几千 token。**

### 5.3 为什么 `list_tools` 里还要再 `sort` 一次

因为 `registry.schemas()` 已经排序了（2.1 §5.7）。**这里是冗余防御**——万一将来有人换了个不排序的 registry 实现，`list_tools` 仍然稳定。

冗余防御值不值得？在这个场景值得：**排序失效的后果是"prompt cache 全部失效"，而你不知道为什么账单涨了。**

### 5.4 工具的 `arguments` 为什么又变回 dict 了

协议里 `params.arguments` 是 **JSON 对象**（不是字符串）。而 2.1 的 `ToolRegistry.call()` 契约是"JSON 字符串"（因为 2.1 是直接从模型的 `tool_calls` 拿的，那里是字符串）。

所以这里要转一次：

```python
outcome = await self.registry.call(name, json.dumps(arguments, ensure_ascii=False))
```

**这层转换在真实项目里就该存在**：协议层（dict）与业务层（字符串）的表示不同，转换点必须显式。

### 5.5 日志级别为什么逐请求读

旧版有 `logging/setLevel`，那是**连接级**设置。现在没有连接了，"这个连接想收什么级别的日志"这个信息无处存放。

于是规范改成：**每个请求自己在 `_meta.logLevel` 里声明**。

实测（§7）：默认 `info` 时看不到 `request_received`；带 `logLevel=debug` 就能看到。

### 5.6 为什么日志必须写 stderr

`stdout` 是**协议通道**。往里打一行日志，客户端读到的就是：

```
{"jsonrpc":"2.0","id":1,"result":{...}}
2026-11-17 23:36:19 [info] tool_called tool=query_asset    ← 这行会让 JSON 解析失败
```

**这是本节的实测事故**，见 §6.1。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：让日志写进 stdout（我真踩了）

写 demo 时，我给 Server 的 `_log` 传了独立的 `log_stream`，**但忘了工具层用的是 structlog**。
它的默认 logger factory 写 stdout。于是 demo 输出里出现了：

```
第 3 步：tools/call —— 复用 2.1 的工具注册表
==============================================================================
2026-11-17 23:36:19 [info     ] tool_called  args_size=23 duration_ms=64.9 ok=True tool=query_asset
  【调用成功】
    {"jsonrpc": "2.0", ...}
```

**如果这是真实 stdio 会话，客户端在这一行就会解析失败**——而且报错是"JSON 解析错误"，
你会去查协议实现，不会想到是日志。

修法：`configure_logging(level="INFO", stream=sys.stderr)`。我给 `configure_logging` 加了
`stream` 参数（默认 None = stdout，兼容前面章节的 CLI 输出）：

```python
logger_factory=structlog.PrintLoggerFactory(file=stream) if stream is not None else structlog.PrintLoggerFactory()
```

修复后重跑，stdout 里只剩协议 JSON，日志全部落到 stderr（§7.2 有实测）。

### 6.2 实验 B：`tools/list` 不排序

`ToolRegistry` 内部用 dict 存工具，**dict 的顺序就是注册顺序**。实测：

```
正序注册 → dict 顺序 : ['query_asset', 'query_permission', 'query_ticket']
反序注册 → dict 顺序 : ['query_ticket', 'query_permission', 'query_asset']
两者 dict 顺序相同吗 : False
sort 之后           : ['query_asset', 'query_permission', 'query_ticket'] / ['query_asset', 'query_permission', 'query_ticket']
sort 之后相同吗     : True
```

**两个团队用同样三个工具、只是注册顺序不同，`tools/list` 的 JSON 序列就不同** → 客户端缓存的 key 变了 → 缓存全部失效 → prompt cache 也失效 → 账单上涨。

而这一切**没有任何报错**。

---

## 7. 验证与预期输出

### 7.1 七步演示（in-process）

```powershell
uv run python -m scripts.mcp_server_demo
```

实测（节选）：

```
第 1 步：server/discover
    支持的版本: ['2026-07-28']
    身份      : {'name': 'servicedesk-mcp', 'version': '0.1.0'}
    能力      : ['prompts', 'resources', 'tools']
    resultType: complete

第 2 步：tools/list
    工具（已排序）: ['query_asset', 'query_permission', 'query_ticket']
    ttlMs=60000  cacheScope=global
    两次调用结果完全一致: True   ← prompt cache 稳定的前提

第 3 步：tools/call
  【调用成功】
    {"result": {"resultType": "complete", "content": [{"type": "text",
      "text": "{\"found\": true, \"hostname\": \"NB-1024\", \"owner\": \"张伟（E-2048）\", ...}"}],
      "isError": false}}
  【参数校验失败（isError=true，但协议层仍是成功响应）】
    {"result": {..., "content": [{"type": "text", "text": "错误：参数校验失败 —— hostname：String should have at least 3 characters"}], "isError": true}}
  【未知工具】
    {"result": {..., "content": [{"type": "text", "text": "错误：没有名为 'query_wifi' 的工具。可用工具：[...]"}], "isError": true}}

第 5 步：Prompts
    prompts/get → {"messages": [{"role": "user", "content": {"type": "text",
      "text": "请用通俗的语言解释工单 T-1001 的处理进度，..."}}]}

第 6 步：错误路径
  未知方法   → code=-32601 message=未知方法 'tools/nonexistent'
  未知资源   → code=-32602 message=未知资源 'doc://nope'
  版本不支持 → code=-32600 message=不支持的协议版本 '2025-11-25'
               data={'supported': ['2026-07-28']}
```

### 7.2 真实 stdio 往返（这是本节的核心验证）

```powershell
$req = @(
  '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}',
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_ticket","arguments":{"ticket_id":"T-1001"}}}',
  'oops not json'
)
$req -join "`n" | uv run python -m servicedesk.mcp.server 2> stdio.err | Set-Content stdio.out
```

实测 stdout（**4 行，全是纯 JSON**）：

```
{"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "protocolVersions": ["2026-07-28"], ...
{"jsonrpc": "2.0", "id": 2, "result": {"resultType": "complete", "tools": [{"name": "query_asset", ...
{"jsonrpc": "2.0", "id": 3, "result": {"resultType": "complete", "content": [{"type": "text", ...
{"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "JSON 解析失败：Expecting value"}}
```

实测 stderr（日志在这里，不污染协议）：

```
[info] tool_called tool=query_ticket ok=True duration_ms=54.2
```

**三件事同时被验证了**：真实子进程 + 管道通信、坏输入不崩（返回 -32700 继续服务）、日志与协议分离。

### 7.3 想用 MCP Inspector 自查

```powershell
npx @modelcontextprotocol/inspector uv run python -m servicedesk.mcp.server
```

Inspector 会以 stdio 方式拉起这个 Server，你能在界面上看到 `server/discover` 的结果与 3 个工具。
（本机没有 Node 环境，未实测——所以本节用 §7.2 的等价验证。）

### 7.4 测试

```powershell
uv run pytest
```

```
........................................................................ [ 49%]
........................................................................ [ 99%]
.                                                                        [100%]
145 passed in 10.73s
```

（1.x 的 78 + 2.1 的 16 + 2.2 的 11 + 2.3 的 20 + 本节的 20。）

---

## 8. 生产环境踩坑实录

### 坑 1：日志写进 stdout，客户端随机解析失败

**现象**

客户端偶尔报"JSON 解析失败"，但重试又好了。抓包看，服务端确实发了合法 JSON。

**定位方法**

把服务端的 stdout 和 stderr 分开重定向，看 stdout 里有没有非 JSON 行：

```powershell
uv run python -m servicedesk.mcp.server 2> err.log 1> out.log
# 逐行验证
Get-Content out.log | ForEach-Object { try { $_ | ConvertFrom-Json | Out-Null } catch { "不是 JSON: $_" } }
```

**根因**

任何 `print()`、任何默认写 stdout 的日志库（structlog 默认就是），都会污染协议通道。
**"偶尔"是因为日志只在某些路径触发**（比如工具调用、异常分支）。

**修法**

```diff
- configure_logging(level="INFO")                    # 默认写 stdout
+ configure_logging(level="INFO", stream=sys.stderr)  # 协议 Server 必须指到 stderr
```

**顺手加固**：在 `serve_stdio` 启动时把 `sys.stdout` 包一层，写非 JSON 内容就断言失败（开发期）。

### 坑 2：工具失败被包成协议错误

**现象**

模型调 `query_asset` 传错参数，客户端直接抛异常、整个对话中断。而按设计，模型应该看到"参数校验失败"然后改参数。

**定位方法**

看响应里有没有 `error` 字段：

```python
if "error" in response:
    print("协议错误：客户端该修代码")
elif response["result"].get("isError"):
    print("工具错误：模型该看内容并自我纠正")
```

**根因**

把 `registry.call()` 的失败结果当成异常抛出，而不是包成 `isError: true` 的结果。

**修法**

```python
return {
    "result": {
        "resultType": "complete",
        "content": [{"type": "text", "text": outcome.content}],
        "isError": not outcome.ok,        # ← 语义在这里，不是抛异常
    }
}
```

### 坑 3：忘了给结果加 `resultType`

**现象**

客户端拿到结果后行为异常：本该"继续"的流程卡住，或把普通结果当成"需要补充输入"。

**根因**

2026-07-28 要求每个结果带 `resultType`（2.3）。漏了之后，**新版客户端按规范应当当作 `complete`**——但如果你自己也写了严格的校验，就会互相打架。

**修法**

在所有返回结果的地方统一加：

```diff
- return {"jsonrpc": "2.0", "id": request_id, "result": result}
+ return {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", **result}}
```

**顺手加固**：把"构造响应"收敛成一个函数（本节的 `handle_async` 末尾就只有一处），
**别让 8 个分支各自拼响应**。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 <code>server/discover</code> 是 Server 必须实现的，而 Client 可以不调？</b></summary>

因为它的作用是"让想知道的人能知道"。Server 必须实现，是为了保证任何客户端在任何时刻都能问到；Client 可以不调，是因为每个请求本身已经带了版本与能力（2.3），不调也能工作。要求"Client 必须先调"就等于把无状态又变回有状态了。

</details>

<details>
<summary><b>Q2：工具执行失败为什么不能返回 JSON-RPC 的 <code>error</code>？</b></summary>

因为 `error` 的语义是"这个请求本身不合法"，客户端收到它应该修代码或报障；而"参数校验失败"是**模型可以自我纠正**的业务失败（2.1 §5.3）。返回 `isError: true` 让模型看到错误文本并重试，返回 `error` 则直接中断对话。

</details>

<details>
<summary><b>Q3：<code>tools/list</code> 为什么必须确定性排序？</b></summary>

因为它是客户端缓存的 key 的一部分，也是 upstream prompt cache 的一部分。实测：同样三个工具，注册顺序一变，`tools/list` 的 JSON 序列就不同（`['query_asset',...]` vs `['query_ticket',...]`），缓存全部失效且不报错。

</details>

### 9.2 动手任务：给 `tools/call` 加 MRTR

**要求**

在 `create_ticket`（新建一个）缺必填参数时，**不要**返回 `isError`，而是按 2.3 的 MRTR 返回：

```json
{"result": {"resultType": "input_required",
            "inputRequests": [{"id": "priority", "prompt": "优先级？", "schema": {"enum": ["P0","P1","P2","P3"]}}]}}
```

客户端重试原请求时带上 `inputResponses`，Server 从里面取值继续。

**验收标准**

- 第一次调用（缺 `priority`）→ `resultType == "input_required"`，`inputRequests` 里有 `priority`。
- 重试（带 `inputResponses: {"priority": "P1"}`）→ `resultType == "complete"`，工单建成。
- 重试时**方法名、id、其余参数都不变**（用 2.3 的 `build_retry_with_inputs`）。
- 补 3 个用例，原有 145 个仍全绿。

**提示**：`_call_tool` 里要检查 `params.get("inputResponses")`——第一次没有、重试有。

### 9.3 拓展挑战

给 Resources 加 `subscriptions/listen` 的雏形：客户端订阅 `resourcesListChanged`，Server 在资源变更时推送通知。

**思考**：

1. 无状态协议下，"订阅"这个长连接该怎么活？它和"每个请求自带上下文"矛盾吗？
2. 如果客户端断线了，它怎么知道**错过**了哪些变更？（提示：想想 2.3 里 SSE 续传为什么被移除）

---

## 10. 本节小结与延伸

**三点回顾**

1. **一个 MCP Server 就是"读 stdin、写 stdout 的循环"**，没有魔法。手写一遍，你才知道 SDK 替你做了什么。
2. **错误分两层**：协议层 `error`（客户端该修代码）vs 工具层 `isError`（模型该自我纠正）。混了就会让"参数写错"变成"对话中断"。
3. **stdout 是协议通道，日志必须走 stderr**。这是我在写 demo 时真的踩到的坑（structlog 默认写 stdout），修法是给 `configure_logging` 加 `stream` 参数。

**已知限制（诚实记录）**

- **未用 MCP Inspector 验证**（本机没有 Node）：用等价的真实 stdio 管道往返替代，见 §7.2。
- **`server/discover` / 各原语的返回体字段名按常规形状实现**，未逐字对照 schema（2.3 已标注同一问题）。
- **只支持新版**：`SUPPORTED_VERSIONS` 只有 `2026-07-28`，没有做 dual-era 双栈（2.3 §8 坑 1 是作业）。
- **没有订阅、没有 MRTR**：留作 §9.2 / §9.3。

**延伸阅读**

- MCP Server 概念：https://modelcontextprotocol.io/specification/2026-07-28/server
- Tools：https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- Resources：https://modelcontextprotocol.io/specification/2026-07-28/server/resources
- Prompts：https://modelcontextprotocol.io/specification/2026-07-28/server/prompts
- MCP Inspector：https://github.com/modelcontextprotocol/inspector

---

## 修订记录

- 2026-11-17 初稿：七步演示与 20 个测试全部实机运行；真实 stdio 管道往返 4 条消息验证通过
- 2026-11-17 修正：`handle()` 改为 `handle_async()`（`tools/call` 需要 await），去掉原先"同步入口 + 异步旁路"的别扭设计
- 2026-11-17 修正：发现工具层 structlog 日志污染 stdout（实测输出可见），给 `configure_logging` 增加 `stream` 参数并要求 MCP Server 传 `sys.stderr`
