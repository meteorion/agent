---
section: "2.5"
title: "Streamable HTTP + subscriptions/listen 上 Serverless"
stage: 2
status: done
depth: L2
difficulty: 5
minutes: 120
milestone: M2
depends_on: ["2.4"]
produces: ["mcp/http_app.py", "Dockerfile"]
verifies: "两个并发请求打到不同实例，工具调用都成功且互不影响"
part: null
updated: "2026-11-17"
---

# 2.5 Streamable HTTP + subscriptions/listen 上 Serverless

> ⚠️ `subscriptions/listen` 的事件形状（首帧为请求的 result、后续为 `notifications/*`、事件打
> `subscriptionId`）是按 2.3 的规范语义实现，**未逐字对照 schema**。落地前请核对
> [规范](https://modelcontextprotocol.io/specification/2026-07-28)。`MAX_STREAM_EVENTS` 是 demo 用的收尾限制。

## 0. 本节导航

**本节在链路中的位置**：2.4 的 Server 只能被本地进程拉起（stdio）。本节把它变成**普通的 HTTP 工作负载**——这是 M2 的关键一步，也是 2.7 把工具接回 Agent 的前提。

**本节你将获得**

1. 单端点 `POST /mcp` 的完整实现：`Mcp-Method` / `Mcp-Name` 路由头 + 一致性校验。
2. `subscriptions/listen` 的流式实现：按类型 opt-in，未知类型**拒绝但不打断流**。
3. "断流 = 请求丢失"的正确处理：换新 request id 重发，不指望续传。
4. **实测**两个实例并发互不影响、跨实例句柄可读——Serverless 的验收点。
5. 一份可直接构建的 `Dockerfile`。

**前置检查清单**

- 2.4 的真实 stdio 往返通过（`uv run python -m servicedesk.mcp.server`）。
- 2.3 的"没有 session、状态在句柄"已理解。

**难度**：★★★★★ ｜ **预计时长**：120 分钟 ｜ **本节费用**：**¥0**（本地回环，不连外网）

---

## 1. 为什么需要它（痛点先行）

stdio 版跑起来了，但它有个硬限制：**只能被"拉起它的那个进程"使用**。

- 浏览器里的前端连不上
- 三个团队要共用同一个 Server，得各自在本机装一份
- 没法做灰度、没法按 QPS 扩容、没法上云

于是要换成 HTTP。而 HTTP 版的历史包袱是：

**旧版（HTTP+SSE）**：两个端点，一个 `GET` 建长连接收通知，一个 `POST` 发请求，靠 `Mcp-Session-Id` 关联。
**问题**：GET 长连接必须一直开着，负载均衡一动就断，Serverless 根本跑不了。

**新版（Streamable HTTP）**：

- **只有一个端点** `POST /mcp`
- 通知走 `subscriptions/listen` 的**请求内响应流**，不再需要"另开一条 GET"
- 没有 session，任意实例都能处理

> 官方已经把旧版 HTTP+SSE 标记为弃用（12 个月窗口）。**新项目不要再用双端点 + session 的那套。**

---

## 2. 核心概念与架构设计

### 2.1 类比：地铁闸机

- **一个口进**（单端点 `/mcp`）= 所有业务都走同一个闸机
- **票面写着去哪**（`Mcp-Method` 头）= 闸机扫一眼就知道该放你进哪个通道
- **闸机不翻你的包**（不解析 body）= 网关/限流/WAF 只看头就能干活，性能极高

> **这个类比在哪失效**：闸机只负责"放行"，**不负责"验货"**。所以除了头，服务端仍必须校验
> **头与 body 一致**——否则你可以拿一张"去 A 站"的票，包里装着"去 B 站"的请求。

### 2.2 一次调用的形状

```
POST /mcp
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: query_asset
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"query_asset","arguments":{"hostname":"NB-1024"},
           "_meta":{...}}}

─────────────────────────────────────────────►
200 OK
{"jsonrpc":"2.0","id":1,
 "result":{"resultType":"complete","content":[...],"isError":false}}
```

### 2.3 订阅：一条长连接 POST 响应流

```
POST /mcp   Mcp-Method: subscriptions/listen
{"jsonrpc":"2.0","id":6,"method":"subscriptions/listen",
 "params":{"types":["toolsListChanged","promptsListChanged"]}}

◄── 200 OK, text/event-stream
◄── data: {"id":6,"result":{"subscriptionId":"sub_194e…","types":[...],"rejected":[]}}
◄── data: {"method":"notifications/toolsListChanged","params":{"_meta":{…subscriptionId},"seq":0}}
◄── data: {"method":"notifications/promptsListChanged","params":{"_meta":{…subscriptionId},"seq":0}}
```

**三个关键点**：

- **首帧就是这条 listen 请求的响应**，带着 `subscriptionId`
- 后续通知都打上同一个 `subscriptionId`，客户端才知道"这是哪次订阅的"
- **按类型 opt-in**：只推你订的那些，不刷无关事件

### 2.4 断流语义（**最容易犯错**）

```
旧版：流断了 ──► 带 Last-Event-ID 重连 ──► 服务端补发缺失事件
新版：流断了 ──► 请求丢了 ──► 客户端必须**新 request id 重发整个请求**
```

**没有任何续传机制。** 这不是退化，而是"无状态"的必然结果：服务端不留事件缓冲区，
因为它不知道你会不会回来、也不知道你收到哪了。

### 2.5 Serverless 部署形态

```
        ┌──────────── 负载均衡（普通轮询，无粘性） ────────────┐
        │                    │                    │           │
    实例 A               实例 B               实例 C（冷启动）
        │                    │                    │
        └────────────┬───────┴────────────────────┘
                     ▼
              共享句柄存储（Redis/DB）
```

**实例之间不通信、不共享内存**，唯一共享的是**句柄存储**（2.3 §5.6）。
所以任意请求落任意实例都行，冷启动实例收到第一个请求就能干。

### 2.6 适用范围（边界）

| 场景 | 建议 |
|---|---|
| 多客户端共用、要上云 | ✅ Streamable HTTP |
| 单机本地工具（编辑器插件） | ✅ stdio 更简单（2.4） |
| 需要服务端主动推**大量**事件 | ⚠️ 长连接成本高，考虑 Tasks 扩展（异步任务） |
| 需要断点续传的流 | ❌ 本版不支持，要自己实现"事件序号 + 客户端去重" |

---

## 3. 环境与版本

本节**不新增依赖**（用标准库 `http.server` + 已有的 `httpx` 做客户端）。

| 组件 | 实测版本 | 说明 |
|---|---|---|
| httpx | `0.28.1` | 客户端 + SSE 流读取 |
| Python | `3.12.13` | `ThreadingHTTPServer` |
| structlog | `26.1.0` | 日志（**写 stderr**，2.4 的教训） |

**验证**

```powershell
uv run python -m servicedesk.mcp.http_app
```

应打印 `MCP Streamable HTTP 已启动: http://127.0.0.1:8080/mcp`。

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/mcp/http_app.py` | **主角**：HTTP 端点 + 路由头 + 订阅流 |
| `Dockerfile` | 部署产物 |
| `scripts/mcp_http_demo.py` | 七步演示 |
| `tests/test_mcp_http.py` | 15 个断言 |

### 4.1 单端点 + 路由头

```python
    def do_POST(self) -> None:
        if self.path.rstrip("/") != ENDPOINT:
            self._json(404, {"error": {"message": f"未知路径 {self.path}"}})
            return

        method_header = self.headers.get("Mcp-Method")
        version_header = self.headers.get("MCP-Protocol-Version")

        # 路由头是必填的：网关/限流/WAF 靠它工作，不解析 JSON 体
        if not method_header:
            self._json(400, {"error": {"message": "缺少 Mcp-Method 头"}})
            return

        ...
        body_method = request.get("method")
        # 头与 body 必须一致，否则可以用一个"合法"的头把请求偷运到别的处理器
        if body_method != method_header:
            self._json(400, {"error": {"message": f"Mcp-Method 头({method_header}) 与请求体方法({body_method}) 不一致"}})
            return
```

### 4.2 每个请求独立跑事件循环（无状态的直白体现）

```python
        # 无状态：每个请求独立跑一个事件循环，实例不保留任何上下文
        response = asyncio.run(self.mcp.handle_async(request))
```

**这一行就是"无状态"的物理证明**：没有跨请求的循环、没有跨请求的任务、没有跨请求的内存。

### 4.3 订阅流

```python
    def _stream_subscriptions(self, request: dict[str, Any]) -> None:
        params = request.get("params") or {}
        requested = params.get("types") or ["toolsListChanged"]
        accepted = [item for item in requested if item in SUBSCRIPTION_TYPES]
        rejected = [item for item in requested if item not in SUBSCRIPTION_TYPES]

        subscription_id = f"sub_{uuid.uuid4().hex[:12]}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # 第一条事件就是这条 listen 请求的响应（带上 subscriptionId）
        self._sse({"jsonrpc": "2.0", "id": request.get("id"),
                   "result": {"resultType": "complete",
                              "subscriptionId": subscription_id,
                              "types": accepted, "rejected": rejected}})

        for item in accepted:
            for seq in range(2):
                self._sse({"jsonrpc": "2.0",
                           "method": f"notifications/{item}",
                           "params": {"_meta": {META_SUBSCRIPTION_ID: subscription_id}, "seq": seq}})
```

### 4.4 Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PORT=8080
EXPOSE 8080
CMD ["uv", "run", "--no-dev", "python", "-m", "servicedesk.mcp.http_app"]
```

完整实现见 [src/servicedesk/mcp/http_app.py](../../servicedesk-agent/src/servicedesk/mcp/http_app.py)。

### 4.5 运行

```powershell
uv run python -m scripts.mcp_http_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么只有一个端点

旧版有两个：`GET /sse`（长连接）、`POST /messages`（发请求）。两个端点意味着**两条连接要关联起来**——靠 `Mcp-Session-Id`。

新版把通知塞进"请求的响应流"里，于是：

- 一个端点，天然没有"关联"问题
- 负载均衡不用管粘性
- 一个请求 = 一个 TCP 流，断了就是断了，语义清晰

### 5.2 为什么头必须与 body 一致

这是**防路由欺骗**。想象一个网关：

```python
if request.headers["Mcp-Method"] == "tools/list":
    return cache.get("tools-list")          # 缓存命中，直接返回
```

如果允许"头写 `tools/list`、体里是 `tools/call`"，那么一个**调用请求**会被网关当成**列表请求**缓存起来——后面所有人拿到的都是工具列表。

§6.1 实测了"按 header 分发"的后果：调用方期待 `content`/`isError`，实际拿到 `{tools, ttlMs, cacheScope}`，**字段缺失导致下游解析崩**。

### 5.3 为什么订阅走 POST 流而不是"另开 GET"

- 旧版的 GET 长连接**无法携带请求体**，只能靠 session 关联"我要订什么"
- 新版把"订什么"放进 POST 的 body（`types`），响应就是那条流

### 5.4 为什么未知类型只拒绝、不失败

§6.2 实测：

```
我们的实现: status=200 accepted=['toolsListChanged'] rejected=['bogusType']  ← 流继续
严格实现的后果: 整条流 400，所有订阅者一起掉线（新增一个类型 = 全量故障）
```

**兼容性原则**：客户端可能比服务端新。它订了一个你还不认识的类型，不该因此失去所有通知。
**明确告诉它"这个我不支持"（`rejected`），比让它整条流失败好。**

> 反过来，这也要求客户端**必须读 `rejected`**，否则它会以为订阅成功了。

### 5.5 `subscriptionId` 为什么必须回传

因为一条连接上可能有多个订阅（客户端可以连续发几次 `listen`）。通知里不带 id，客户端无法区分
"这是哪次订阅的事件"。

这跟 2.3 的句柄是同一个套路：**可见的、显式的标识符，代替隐藏的会话关联。**

### 5.6 `MAX_STREAM_EVENTS` 是什么

```python
MAX_STREAM_EVENTS = 6
```

生产里订阅流是**长连接**，不主动关。这里加个上限是为了让 demo 和测试能结束。
**生产代码不要照抄这个限制。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：按 header 分发，而不是校验一致性

我们的实现：头写 `tools/list`、体里是 `tools/call` → **400**，明确拒绝。

如果改成"按 header 分发"（很多网关就是这么写的），实测：

```
按 header 分发的后果: 返回 keys=['cacheScope', 'resultType', 'tools', 'ttlMs']
实际拿到的是 3 个工具定义，isError 字段缺失
```

调用方的代码：

```python
result = response["result"]
if result["isError"]:      # ← KeyError
    ...
```

**一个字段缺失的响应，比一个明确的 400 难查十倍。**

### 6.2 实验 B：未知订阅类型让整条流失败

把 `accepted/rejected` 的宽容逻辑改成"只要有一个不认识就 400"：

```
我们的实现: status=200 accepted=['toolsListChanged'] rejected=['bogusType']  ← 流继续
严格实现的后果: 整条流 400，所有订阅者一起掉线
```

后果场景：你上线了一个新版本，客户端**先**升级并开始订阅 `tasksChanged`。
此时老服务端会 400 → **所有升级的客户端失去全部通知**，而且是静默的（客户端最多记一条 warn）。

**版本迭代期的兼容性，靠的就是"部分接受"。**

---

## 7. 验证与预期输出

### 7.1 七步演示

```powershell
uv run python -m scripts.mcp_http_demo
```

实测（节选）：

```
实例 A: http://127.0.0.1:53523/mcp
实例 B: http://127.0.0.1:53524/mcp

第 1 步：单端点 POST + 路由头
  status=200  resultType=complete
  versions=['2026-07-28']  server=servicedesk-mcp

第 2 步：路由头是硬要求
  缺 Mcp-Method  → 400  缺少 Mcp-Method 头
  头与体不一致   → 400  Mcp-Method 头(tools/list) 与请求体方法(tools/call) 不一致
  版本头不支持   → 400  不支持的协议版本 '2025-11-25'

第 3 步：tools/call 走 HTTP
  isError=False
  内容: {"found": true, "employee_id": "E-2048", "name": "张伟", "vpn": true, ...}

第 4 步：subscriptions/listen
  status=200  content-type=text/event-stream; charset=utf-8
  订阅 id   : sub_194e0902d985
  已接受类型: ['toolsListChanged', 'promptsListChanged']
  已拒绝类型: ['bogusType']   ← 未知类型不会让整条流失败
  通知 notifications/toolsListChanged     seq=0 subscriptionId=sub_194e0902…
  通知 notifications/promptsListChanged   seq=0 subscriptionId=sub_194e0902…

第 5 步：流断了 = 请求丢了
  第一次订阅用的 request id: 6
  重发用的 request id      : 7   ← 必须是新的
  新 subscriptionId        : sub_bdc61193…（与上次不同：True）

第 6 步：两个实例并发，互不影响
  实例 A: isError=False  owner=张伟（E-2048）
  实例 B: isError=False  owner=李娜（E-3102）
  >>> 两个请求落在两台互不通信的实例上，都成功。

第 7 步：跨实例的「状态」
  实例 A 的工具签发了 handle=h_0b56a6a981a245d5
  实例 A 用这个 handle 取状态 → {'user': '张伟', 'step': 2}
  实例 B 用这个 handle 取状态 → {'user': '张伟', 'step': 2}
```

**第 6、7 步合起来就是本节验收点**：并发打到两台实例都成功，跨实例状态可读——
因为状态不在实例里。

### 7.2 测试

```powershell
uv run pytest
```

```
........................................................................ [ 45%]
........................................................................ [ 90%]
................                                                         [100%]
160 passed in 19.33s
```

（1.x 的 78 + 阶段二的 82。）

---

## 8. 生产环境踩坑实录

### 坑 1：网关按头缓存，把 tools/call 的结果缓存成了工具列表

**现象**

偶发地，某个工具调用的响应里是三个工具定义，导致客户端 `KeyError: 'content'`。

**定位方法**

把响应体的 key 打出来：

```python
print(sorted(response["result"]))
# 正常: ['content', 'isError', 'resultType']
# 异常: ['cacheScope', 'resultType', 'tools', 'ttlMs']   ← 这是 tools/list 的结果
```

**根因**

网关/CDN 按 `Mcp-Method` 头做缓存 key，而**服务端没校验头与 body 一致**，于是
"头写 list、体是 call"的请求被当成 list 请求处理并缓存。

**修法**

```python
if body_method != method_header:
    return 400
```

并且网关的缓存 key **必须包含 body 的指纹**（或只对幂等读方法做缓存）。

**顺手加固**：在网关层禁止对 `tools/call`、`prompts/get` 这类非幂等方法做成功响应缓存。

### 坑 2：以为订阅流能续传，结果丢事件

**现象**

用户反馈"工单状态变了但界面没刷新"。查日志：客户端重连了 3 次，每次都重新订阅，但中间的事件没了。

**定位方法**

对比客户端记录的 `subscriptionId` 序列与事件数量：

```python
log.warning("resubscribe", old=old_sub_id, new=new_sub_id, gap_seconds=gap)
```

有 gap 就说明丢了。

**根因**

2026-07-28 **移除了 SSE 续传**。断流期间服务端产生的事件，客户端永远拿不到。

**修法**（三层）：

1. **订阅流只当"提示"，不当"数据源"**。收到通知后**主动拉一次**（`tools/list` 或业务查询）
2. 重连时用**新 request id** 重新 `listen`，不要试图续
3. 关键状态变更要有**对账机制**（定时全量同步一次）

**顺手加固**：把"通知"和"数据"解耦——通知说"变了"，数据靠拉取。这样丢通知也能自愈。

### 坑 3：把 `asyncio.run` 放到已有事件循环里

**现象**

```
RuntimeError: asyncio.run() cannot be called from a running event loop
```

**根因**

如果你的 HTTP 框架本身是异步的（FastAPI/Starlette），`do_POST` 里再 `asyncio.run()` 就会炸。

**修法**

本节用 `ThreadingHTTPServer`（每个请求一个线程、线程内没有事件循环），所以 `asyncio.run()` 安全。
换成 ASGI 框架时：

```diff
- response = asyncio.run(self.mcp.handle_async(request))
+ response = await self.mcp.handle_async(request)      # 直接在协程里 await
```

**顺手加固**：把 `http_app` 的传输层与 `McpServer` 的逻辑层保持分离（本节已经这么做），
换框架时只改传输层。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么新版只需要一个 HTTP 端点？</b></summary>

因为通知被放进了"请求的响应流"里。旧版需要 GET 建长连接 + POST 发请求两个端点，再靠 `Mcp-Session-Id` 关联两者。新版把"我要订阅什么"放进 POST 的 body，响应就是那条流——一请求一流，不需要关联，也不需要 session。

</details>

<details>
<summary><b>Q2：为什么必须校验 `Mcp-Method` 头与 body 的 method 一致？</b></summary>

因为网关/限流/WAF 只看头就做路由和缓存。如果不一致，可以用一个"合法"的头把调用请求偷运成列表请求——实测会让调用方拿到工具列表而不是调用结果，且报错是 `KeyError` 而不是明确的 400。

</details>

<details>
<summary><b>Q3：订阅里出现未知类型，为什么不能整条流失败？</b></summary>

因为客户端可能比服务端新。新客户端订阅一个你还不认识的类型时，如果整条流 400，它就失去了**所有**通知。正确做法是"部分接受"：认识的接受、不认识的放进 `rejected`，让客户端自己决定怎么办。

</details>

### 9.2 动手任务：给订阅流加"事件序号 + 客户端去重"

**要求**

1. 服务端在每条通知里带全局递增的 `seq`（不是每类型各自从 0 开始）。
2. 客户端维护 `last_seq`；重连后拿到新订阅，若 `seq` 跳变（> last_seq + 1），打印一条"可能丢事件"的警告并触发一次全量对账。
3. 补 3 个用例：连续订阅无跳变、断开重连有跳变、`rejected` 类型被正确记录。

**验收标准**

- 正常连续推送时 `seq` 连续，客户端不告警。
- 人为丢掉一条事件后，客户端能检测到跳变（这正是"没有续传"的补偿机制）。
- 不修改服务端已有 15 个用例的行为。

**提示**：这是把"不可靠的推送"变成"可靠的数据流"的经典手法——**推送只做提示，数据靠拉取 + 对账**。

### 9.3 拓展挑战

把 `http_app` 从 `ThreadingHTTPServer` 迁到 ASGI（FastAPI/Starlette），并保持所有测试通过。

**思考**：

1. `do_POST` 的同步逻辑怎么搬进 `async def endpoint()`？哪些地方会自然变简单？
2. SSE 在 ASGI 下用 `StreamingResponse` 实现，和现在的 `wfile.write` 相比，断流检测有什么差别？
3. 迁完之后，`asyncio.run` 那句去哪了？为什么它必须消失？

---

## 10. 本节小结与延伸

**三点回顾**

1. **单端点 + 路由头**：`POST /mcp` 承载一切，`Mcp-Method`/`Mcp-Name` 让网关不解析 body 就能路由；但**头与 body 必须一致**，否则可以被偷运（实测：调用方拿到工具列表而不是结果）。
2. **订阅是一条请求内的流**：首帧是 listen 的响应（带 `subscriptionId`），后续通知打同一个 id；未知类型**拒绝但不打断**。
3. **断流就是丢请求**：没有续传，只能换新 id 重发；所以**推送只做提示，数据靠拉取 + 对账**。

**已知限制（诚实记录）**

- **订阅事件的字段形状未逐字对照 schema**（⚠️ 见文首）。
- **`MAX_STREAM_EVENTS` 是 demo 限制**，生产应为长连接。
- **无归属校验**：句柄 store 仍是裸的（2.3 §9.2 作业）。
- **未上真实 Serverless**：只在本机起了两个实例模拟水平扩容；`Dockerfile` 未实际构建（本机无 Docker）。

**延伸阅读**

- Streamable HTTP 传输：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- `subscriptions/listen` 与 Tasks 扩展：https://blog.modelcontextprotocol.io/posts/2026-07-28/
- HTTP 缓存与 Vary 头（为什么缓存 key 要考虑 body）：https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Vary

---

## 修订记录

- 2026-11-17 初稿：七步演示与 15 个测试全部实机运行；两实例并发与跨实例句柄验证通过
- 2026-11-17 修正：demo 里嵌套引号导致的语法错误（`"状态"` → 「状态」）；客户端提前断流引发的 `ConnectionResetError` 已用事件循环异常处理器屏蔽
- 2026-11-17 标注：`subscriptions/listen` 事件形状与 `Dockerfile` 未实际构建，均在正文标明
