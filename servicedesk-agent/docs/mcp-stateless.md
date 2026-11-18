# MCP 2026-07-28 无状态改造：握手与 Session 去了哪

> 本文是 2.3 的交付物。它回答三个问题：
> **删掉了什么？状态挪到哪去了？为什么这版能在 Serverless 上冷启动？**
>
> 依据：官方 [Key Changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、
> [Versioning 页](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)、
> [发布 blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)。
> 标注 ⚠️ 的条目为"正文未逐字核实、按语义推断"，落地前请对照 [schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)。

---

## 1. 两张时序图

### 旧版（2025-11-25 及更早）：先握手，再干活

```
Client                                    Server
  │                                          │
  │ ① initialize (protocolVersion, caps)     │
  │ ────────────────────────────────────────►│
  │ ② InitializeResult                       │
  │ ◄────────────────────────────────────────│  ← 建立 session
  │    (Mcp-Session-Id: 7f3a…)               │
  │ ③ notifications/initialized              │
  │ ────────────────────────────────────────►│
  │ ④ tools/list                             │
  │ ──── Mcp-Session-Id: 7f3a… ─────────────►│
  │ ⑤ tools/call                             │
  │ ──── Mcp-Session-Id: 7f3a… ─────────────►│
```

**致命约束**：④⑤ 必须打到**同一台实例**，否则 session 不存在。
→ 需要粘性路由，或把所有 session 放进共享存储（Redis / 数据库）。
→ 冷启动实例手上没有别人的 session，于是**扩容解决不了问题**。

### 新版（2026-07-28）：每个请求自带全部上下文

```
Client                                    Server（任意一台实例）
  │                                          │
  │ ① server/discover（可选，想提前知道能力才调）│
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

**没有 session**，所以 ③④ 可以落在任何一台实例上。**普通轮询负载均衡就够了。**

一个真实的请求体（来自官方 blog）：

```http
POST /mcp HTTP/1.1
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: search

{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"search","arguments":{"q":"otters"},
 "_meta":{"io.modelcontextprotocol/clientInfo":{"name":"my-app","version":"1.0"}}}}
```

---

## 2. 删掉了什么

| 旧版有 | 新版 | 替代方案 |
|---|---|---|
| `initialize` / `notifications/initialized` 握手 | ❌ 移除 | 每个请求在 `_meta` 里声明版本/身份/能力 |
| `Mcp-Session-Id` 头 | ❌ 移除 | 服务端签发**显式句柄**，由模型当参数带回 |
| 列表接口随连接变化 | ❌ 移除 | `tools/list` 等不再随连接变化 |
| `ping` | ❌ 移除 | — |
| `logging/setLevel` | ❌ 移除 | `_meta` 的 `io.modelcontextprotocol/logLevel` 逐请求设置 |
| `notifications/roots/list_changed` | ❌ 移除 | — |
| 服务端主动发 `sampling/createMessage` 等 | ❌ 改为 MRTR | 服务端返回 `resultType: "input_required"` |
| 结果无类型字段 | ✅ 新增 `resultType` | `"complete"` / `"input_required"` |
| HTTP GET 端点 + `resources/subscribe` | ❌ 移除 | 单一 `subscriptions/listen` 长连接流，按类型 opt-in |
| SSE 断线续传（`Last-Event-ID`、事件 ID） | ❌ 移除 | 断了就是请求丢了，客户端用**新 request id 重发** |
| `tasks/*` 在核心协议里，`tasks/result` 阻塞 | ❌ 移出核心 | 官方扩展 `io.modelcontextprotocol/tasks`：`tasks/get` 轮询 + `tasks/update` |
| 列表结果不可缓存 | ✅ 新增 `ttlMs`、`cacheScope` | `tools/list` 等要求**确定性排序**，利于 prompt cache |
| （无） | ✅ 新增 `server/discover` | Server 必须实现，广播版本/能力/身份 |

**整体弃用（12 个月窗口，仍可用）**：Roots、Sampling、Logging；legacy HTTP+SSE 传输。
**鉴权侧**：RFC 9207 `iss` 校验；Dynamic Client Registration（DCR）正式弃用，转向 CIMD；凭据绑定签发方。

---

## 3. 状态挪到哪去了

删掉 session 不等于"你的应用必须无状态"。官方给的方向是：

> 如果服务端需要跨调用保留状态，就让工具**签发一个显式句柄**，
> 让模型把它当普通参数带回来。这比把状态藏在传输层更好——
> 模型能看见它、能在工具之间传递、能重放、能调试。

三种承载方式，按推荐度排序：

| 方式 | 放在哪 | 优点 | 代价 |
|---|---|---|---|
| **服务端签发句柄**（推荐） | 业务存储（Redis/DB），句柄本身只是一串 id | 可审计、可过期、模型可见 | 要设计 id 与 TTL |
| **请求内 `_meta`** | 每次请求自带 | 完全无状态，最简单 | 内容有大小与敏感信息风险 |
| **会话外置** | 共享存储 + Cookie/Header 关联 | 迁移成本最低 | 仍是"隐藏状态"，调试困难 |

### 句柄的最小实现

```python
class HandleStore:
    def mint(self, data: dict) -> str:      # 工具里调用，返回 h_xxxx
        ...
    def resolve(self, handle: str) -> dict | None:   # 下次调用时用它取回状态
        ...
```

**关键约束**：句柄必须**可过期**、**不可猜测**（别用自增 id）、**只代表服务端自己发的状态**
（不能让客户端伪造一个 id 去读别人的数据）。

---

## 4. 兼容矩阵

规范用三个术语：**Modern**（2026-07-28 及以后，逐请求元数据）、
**Legacy**（2025-11-25 及更早，`initialize` 握手）、**Dual-era**（两者都支持）。

⚠️ 下表是按上述术语与规范正文推导出的**操作含义**，不是规范原文表格的逐字复制：

| Client | Server | 结果 | 谁需要做什么 |
|---|---|---|---|
| Modern | Modern | ✅ 正常 | 无 |
| Modern | Legacy | ✅ 可工作（走旧通道） | Client 需按 `server/discover` 或首次失败退化为 legacy |
| Legacy | Modern | ⚠️ 仅当 Server 是 dual-era | Server 需保留 `initialize` 路径至少一个弃用周期 |
| Modern | Dual-era | ✅ 优先走 modern | Client 先 `server/discover` 选最高版本 |
| Legacy | Modern-only | ❌ 失败 | 必须升级客户端 |

**Dual-era 是过渡期的现实选择**：生态里大量 2025-11-25 的 Server 还在跑，
一次性切过去会打断所有老客户端。规范给了 12 个月弃用窗口，正是为此。

---

## 5. 为什么这版能 Serverless 冷启动

把四件事放在一起看：

1. **没有握手** → 一个冷实例收到第一个请求就能直接处理，不用先"建立会话"。
2. **没有 session id** → 不需要粘性路由，不需要共享会话存储。
3. **request 自描述** → 版本、身份、能力都在请求里，实例不需要"记得"上一轮谈过什么。
4. **列表可缓存 + 确定性排序** → client 可缓存工具目录，不必每次重连都重新拉。

**结论**：MCP Server 变成了一个**普通的 HTTP 工作负载**——可以放在 Lambda / Cloudflare Workers /
任何无状态容器里，按请求计费、按 QPS 扩容、随时冷启动。

> 反过来说：如果你在 2026-07-28 上做 Server，却仍需要粘性会话或共享 session 存储，
> 说明你把状态放错了地方——**应该改成句柄**。

---

## 6. 迁移检查表

从 2025-11-25 迁到 2026-07-28：

- [ ] 删除 `initialize` / `initialized` 处理；改为读每个请求 `_meta`
- [ ] 删除 `Mcp-Session-Id` 的签发与校验
- [ ] 实现 `server/discover`（**必做**，广播支持的版本/能力/身份）
- [ ] 版本不匹配时返回 `UnsupportedProtocolVersionError`，并**列出支持的版本**
- [ ] 所有结果补 `resultType`（`"complete"` / `"input_required"`）
- [ ] 客户端：读取 `resultType` 时**缺失按 `complete` 处理**
- [ ] 把服务端主动请求（sampling / elicitation / roots）改成 MRTR：返回
      `input_required` + `inputRequests`，客户端重试原请求带 `inputResponses`
- [ ] `logging/setLevel` → `_meta.logLevel`；`ping` 直接删除
- [ ] HTTP GET 端点与 `resources/subscribe` → `subscriptions/listen`（按类型 opt-in）
- [ ] 确认已移除对 SSE 断线续传的依赖；断流后用**新 request id** 重发
- [ ] `tools/*` 请求补 `Mcp-Method` / `Mcp-Name` 头
- [ ] `tools/list` 等列表返回加 `ttlMs` / `cacheScope`，并保证**确定性排序**
- [ ] `tasks/*` 若在用 → 迁到 `io.modelcontextprotocol/tasks` 扩展
- [ ] `_meta` 透传 `traceparent` / `tracestate` / `baggage`

---

## 7. 常见误读

| 误读 | 事实 |
|---|---|
| "无状态 = 不能有业务状态" | 可以有，但要显式化（句柄 + 业务存储），不是藏在传输层 |
| "`server/discover` 是必须调的前置" | Server **必须实现**，Client **可以不调** |
| "没有 session 就不能做多轮工具调用" | 多轮靠 `messages` 与句柄，不靠 session |
| "移除 SSE 续传 = 流式不可用" | 流式仍可用，只是**不再保证断线续传**，断了要重发 |
| "`resultType` 是可选字段" | 对**新版结果**是必需的；对客户端读**旧版结果**要容错 |

---

## 8. 一页速查

- 握手 → 逐请求 `_meta`
- session → 显式句柄（server-minted handle）
- 服务端主动问 → MRTR（`input_required` + `inputResponses`）
- 订阅 → `subscriptions/listen`
- 续传 → 没有了，重发
- 结果类型 → `resultType`，缺失按 `complete`
- 缓存 → `ttlMs` / `cacheScope`，列表确定性排序
- 意义 → 任意请求可落任意实例，Serverless 成立
