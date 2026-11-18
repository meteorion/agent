---
section: "5.5"
title: "用 W3C traceparent 串起全链路"
stage: 5
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M5
depends_on: ["5.4"]
produces: ["obs/tracing.py"]
verifies: "一条 trace 能跨进程串起来（含 MCP 调用）"
part: null
updated: "2026-11-18"
---

# 5.5 用 W3C traceparent 串起全链路

## 0. 本节导航

**本节在链路中的位置**：3.6 给了**单进程内**的 `trace_id`（contextvars）。本节点把它升级成**跨进程、跨服务**的标准——用 W3C `traceparent` 头。

**本节你将获得**

1. `traceparent` 的四个字段与解析/格式化（含**宽容失败**的规则）。
2. 派生规则：**`trace_id` 一路不变，`span_id` 每跳换新，`parent` 指回上一跳**。
3. 采样语义：`flags=00` 时**透传但不记录**。
4. 实测：客户端 → 服务 A → 服务 B（三个进程）+ MCP Server，**全部落在同一条 trace 上**，并还原成调用树。
5. 两个我自己踩的语义错误（都很典型：解析结果的 `span_id` 到底是谁的、`inject` 该传哪个上下文）。

**前置检查清单**

- 3.6 的 `uv run python -m scripts.streaming_demo` 能跑通（trace_id 在日志里）。
- 2.5 的 MCP HTTP Server 能跑通。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（本地多进程）

---

## 1. 为什么需要它（痛点先行）

3.6 的 `trace_id` 只在**一个进程内**有效。一旦请求跨了服务：

```
用户 → 网关 → Agent 服务 → MCP Server → 下游工具
```

**每一跳都是新进程**，各自的日志里各有一串 id。用户报"这次很慢"，你要回答：

- 慢在哪一跳？
- 是 Agent 想太久，还是 MCP Server 卡住，还是下游工具超时？

**如果每一跳的 trace_id 都不一样，这个问题无法回答。**

W3C `traceparent` 就是解决这件事的标准：**每一跳都沿用同一个 trace_id**，
各自记录自己的耗时（span），最后拼成一棵树。

---

## 2. 核心概念与架构设计

### 2.1 类比：接力赛的号码布

- **`trace_id` = 队伍编号**：全队从头到尾用同一个
- **`span_id` = 每一棒选手的编号**：每人不同
- **`parent_id` = 我把棒交给了谁**

> **这个类比在哪失效**：接力是线性的；真实调用是**树状**的
> （一个请求可能并发调三个下游）。所以 `span_id` 会有多个子节点，
> 而不是简单的"上一棒"。

### 2.2 四个字段

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             │  │                                │                │
             │  │                                │                └─ flags（01=采样）
             │  │                                └─ parent-id（16 hex）
             │  └─ trace-id（32 hex，一路不变）
             └─ version（00）
```

**三个硬规则**（规范要求）：

1. `trace-id` 与 `span-id` **不能全零**
2. `flags` 是 16 进制，最低位表示是否采样
3. **解析失败就当没收到**——自己起一条新链路，不能 500

### 2.3 派生与透传

```
客户端：  root = new_trace()            trace=T  span=S1  parent=None
          inject(headers, S1)           →  header 里放 T-S1-01
服务 A：  incoming = parse(header)      trace=T  span=S1（这是上游的跨度）
          mine = incoming.child()       trace=T  span=S2  parent=S1  ✓
          inject(down, mine)            →  header 里放 T-S2-01
服务 B：  incoming = parse(header)      trace=T  span=S2
          mine = incoming.child()       trace=T  span=S3  parent=S2  ✓
```

**关键理解**：`parse_traceparent()` 返回的 `span_id` 是**上游的跨度**，
本地要记录自己的跨度必须 `.child()`。

### 2.4 实测：跨三个进程的调用树

```
共 3 个 span，1 条 trace

client.request  (297.3ms)
  └─ svc-a.handle  (148.9ms)
    └─ svc-b.handle  (0.0ms)
```

- **一条 trace**：三个进程用同一个 `trace_id`
- **父子关系正确**：A 的 parent 是客户端 span，B 的 parent 是 A
- **耗时可比**：客户端 297ms 里有 149ms 花在 A 上，A 里又调了 B

### 2.5 实测：含 MCP 调用

```
共 2 个 span，1 条 trace

client.tool_call  (117.3ms)
  └─ mcp.handle  (66.9ms)
```

**5.2 的 MCP Server 也接进了同一条链路**——工具调用不再是黑盒。

> 这是本节对 2.5 的一个小改造：在 `http_app` 的 `_json()`（所有 JSON 响应的唯一出口）
> 里收口 span。**改造只有几行，因为出口是收敛的。**

### 2.6 适用范围（边界）

| 场景 | 要不要 trace |
|---|---|
| 跨多个服务/进程 | ✅ 必须 |
| 单进程单请求 | ⚠️ 3.6 的 trace_id 就够 |
| 需要算每一跳耗时 | ✅ 必须 |
| 高 QPS 且预算紧 | ✅ 但要用采样（§5.4） |

---

## 3. 环境与版本

本节**不新增依赖**（标准库 + `httpx`）。

| 组件 | 实测 |
|---|---|
| Python | `3.12.13` |
| 规范 | W3C Trace Context（`traceparent` = version-traceid-spanid-flags） |

**验证**

```powershell
uv run python -m scripts.tracing_demo
```

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/obs/tracing.py` | **主角**：解析/派生/透传/span/树还原 |
| `src/servicedesk/mcp/http_app.py` | 加了 3 行：在 `_json()` 里收口 span |
| `scripts/trace_service.py` | 可透传的子进程服务 |
| `scripts/tracing_demo.py` | 七步演示 |
| `tests/test_tracing.py` | 27 个断言 |

### 4.1 解析（宽容失败）

```python
def parse_traceparent(header: str | None) -> TraceContext | None:
    """解析 `traceparent`。**任何不合规都返回 None**（而不是抛异常）。

    为什么要"宽容失败"：一个畸形 header 不该让整个请求 500。
    规范也说：**无法解析就当没收到，自己起一条新链路**。
    """
    ...
    # 解析结果代表「上游的跨度」：它的 span_id 就是我们的 parent。
    # 本地要记录自己的跨度时，用 `ctx.child()` 派生——**不要直接拿它当自己**。
    return TraceContext(trace_id=trace_id, span_id=parent_id, parent_id=None,
                        sampled=bool(flag_value & 0x01))
```

### 4.2 派生

```python
    def child(self) -> TraceContext:
        """派生下一跳：**trace_id 不变，span_id 换新，parent 指向我。**"""
        return TraceContext(trace_id=self.trace_id, span_id=_new_span_id(),
                            parent_id=self.span_id, sampled=self.sampled)
```

### 4.3 采样 = 透传但不记录

```python
    def _finish(self, span: Span) -> None:
        # 采样为 false 时不记录——**透传但不算开销**
        if not span.ctx.sampled:
            return
```

### 4.4 树还原

```python
def build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 span 列表还原成调用树：按 parent_id 挂靠，按开始时间排序。

    **找不到父的 span 会被当成根**（跨进程时某一跳没落盘，这是常态，不能因此丢数据）。
    """
```

### 4.5 运行

```powershell
uv run python -m scripts.tracing_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么畸形 header 要"当没收到"

实测（第 3 步）：5 个样本，只有合法的那个被接受。

```
✅ 接受  00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
⛔ 丢弃  00-00000000000000000000000000000000-00f067aa0ba902b7-01   （全零 trace）
⛔ 丢弃  00-xyz-00f067aa0ba902b7-01                                （非法字符）
⛔ 丢弃  01-4bf9...-00f067aa0ba902b7-01                             （未知版本）
⛔ 丢弃  garbage
```

**为什么不能抛异常**：这个 header 可能是：
- 用户手写的 curl
- 某个中间件加的畸形头
- 攻击者构造的

**任何一种都不该让请求 500。** 观测是"顺带做的事"，不能成为可用性的负担。

### 5.2 为什么 `trace_id` 必须一路不变

这是 `traceparent` 存在的**唯一理由**。

**反例**（我第一版的 bug，§8.1）：每跳生成新 `trace_id` → 日志里三串 id →
**无法回答"这次请求经过了哪些服务"**。

**判断方法**：拿最终日志里的 `trace_id` 去搜，应该能搜到**所有服务**的日志。
搜不到，就是断链了。

### 5.3 采样：`flags=00` 时到底要不要记录

实测（第 4 步）：

```
采样=true  记录 span 数: 1
采样=false 记录 span 数: 0
tracer 里总共: 1 个
```

**但 `flags=00` 的请求仍然要透传 header**：

- 上游可能**采样了**（它想看这条链路），只是**这一跳不想付记录成本**
- 如果你把 header 丢了，下游就断链了——上游会看到一棵断掉的树

**一句话：透传是义务，记录是选择。**

### 5.4 为什么 `inject` 要传"我自己的 span"

见 §8.2。**下发时要放自己的 `span_id`**（下游的 parent 就是我）。
如果放 `child().span_id`，下游的 parent 会指向一个**从未被记录的 span**，
树就断在那里。

**记法**：`parse` 得到的是"我的父"，`inject` 放的是"我是谁的父"。

### 5.5 `build_tree` 为什么把孤儿当根

跨进程场景里，"某一跳的 span 没落盘"是**常态**：

- 那一跳的导出失败
- 那一跳没采样
- 那一跳还在飞（你先读了文件）

**如果丢掉孤儿，你会少看到一半的链路。** 当根处理至少能看到"这里有一跳"。

### 5.6 为什么 span 在 `_json()` 里收口

因为 `_json()` 是**所有 JSON 响应的唯一出口**——包括 400/401/403/404 那些提前返回。

如果只在正常路径记 span，**慢请求/失败请求——恰恰是你最想看的——反而没有 span**。

**设计原则**：可观测代码要挂在"收敛的出口"，而不是散在各分支里。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：每跳生成新 `trace_id`

把 `child()` 改成 `new_trace()`：

```
客户端 trace: aaaa…
A 返回 trace: bbbb…   一致=False
B 返回 trace: cccc…
```

**后果**：日志里三串互不相关的 id。用户报障时，你只能一个服务一个服务地问"你见过这个请求吗"。

### 6.2 实验 B：解析失败时抛异常

把 `parse_traceparent` 的 `return None` 改成 `raise ValueError`：

**后果**：任何人用一个畸形 header 就能让你的服务 500。**观测设施变成了攻击面。**

### 6.3 实验 C：不采样也记录

把 `_finish` 里的采样判断去掉：

**后果**：上游说"这条链路不用采"（可能是压测流量、健康检查），你照记不误 →
存储成本和 QPS 成正比，观测费用失控。

**采样是"上游的意愿"，不是"你的能力"**——你没资格替它决定。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.tracing_demo
```

实测（节选）：

```
第 1 步：traceparent 就四个字段
  生成的 header: 00-1fae2e4f5adddeec2460045c1624fcad-768a5bdf8e3935f8-01
  拆开看：
    version  00
    trace-id 1fae2e4f5adddeec2460045c1624fcad
    parent   768a5bdf8e3935f8
    flags    01
  解析结果的 span_id 就是**上游的跨度**（768a5bdf…），
  本地要记录自己的跨度时用 `.child()`：得到 22de9286…，
  它的 parent_id 指回上游（768a5bdf…）。

第 2 步：派生下一跳
  root  trace=a53339e8… span=062f5024… parent=None
  A     trace=a53339e8… span=7af923b0… parent=062f5024…
  B     trace=a53339e8… span=eb8094d6… parent=7af923b0…
  三者 trace_id 相同: True

第 3 步：畸形 header 要宽容处理
  ✅ 接受  00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
  ⛔ 丢弃  00-00000000000000000000000000000000-00f067aa0ba902b7-01
  ...

第 5 步：跨进程 —— 客户端 → 服务 A → 服务 B
  客户端 trace_id: d644be032c65ec269c2a040cf73e1f08
  A 返回   trace_id: d644be032c65ec269c2a040cf73e1f08   一致=True
  A 的 parent: 02d85a65…  是客户端 span: True

第 7 步：把 span 还原成调用树（跨服务）
  共 3 个 span，1 条 trace

  client.request  (297.3ms)
    └─ svc-a.handle  (148.9ms)
      └─ svc-b.handle  (0.0ms)

第 6 步：跨进程且含 MCP 调用
  MCP 调用返回: isError=False
  记录到的 span: ['mcp.handle', 'client.tool_call']
  span 的 parent 是客户端 span: True

第 7 步（MCP）
  共 2 个 span，1 条 trace

  client.tool_call  (117.3ms)
    └─ mcp.handle  (66.9ms)
```

跑测试：

```powershell
uv run pytest
```

```
......................................................................   [100%]
502 passed in 50.75s
```

---

## 8. 生产环境踩坑实录

### 坑 1：解析结果的 `span_id` 到底是谁的（我第一版就错了）

**现象**

跨服务跑完，`build_tree` 出来是三条**平级**的根，不是一棵树。

**定位方法**

打印每一跳的 `trace_id / span_id / parent_id`：

```
A 的 parent: 145f8161…  是客户端 span: True     ← 修复后
A 的 parent: （随机值）  是客户端 span: False    ← 修复前
```

**根因**

```python
# 错：解析时就生成新 span_id，并把上游当成 parent
return TraceContext(trace_id=..., span_id=_new_span_id(), parent_id=parent_id_from_header)
```

于是调用 `.child()` 时，parent 变成了**那个随机生成的 span_id**——
而它从未被记录过，树就断了。

**正确语义**：**解析结果 = 上游的跨度**（`span_id` 用 header 里的），
本地跨度由 `.child()` 派生。

**顺手加固**：写一条断言 `parsed.span_id == header 里的 parent 字段`，
把这个语义钉死。

### 坑 2：`inject` 传了 child 而不是自己

**现象**

同上——A 和 B 各自成了根，B 的 parent 指向一个不存在的 span。

**根因**

```python
child = mine.child()
inject(headers, child)      # ← 放的是 child 的 span_id
```

下游的 parent 就是 `child.span_id`，而 `child` 只是一个中间上下文，
**从未被记录成 span**。

**修法**

```diff
- inject(headers, child)
+ inject(headers, mine)     # 下游的父就是我
```

**记法**：`parse` 得到"我的父"，`inject` 放"我是谁的父"。

### 坑 3：只在正常路径记 span

**现象**

慢请求、失败请求在调用树里**看不到**。

**根因**

span 记录写在"成功分支"里，提前返回（400/401/429）的路径没有 span。

**修法**

挂在收敛的出口（本节的 `http_app._json()`），所有响应都经过它。

**顺手加固**：把 `status` 作为 span 的 `attributes`，这样能直接从树里筛出 5xx。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 <code>trace_id</code> 必须一路不变，而 <code>span_id</code> 每跳换新？</b></summary>

`trace_id` 是"整条请求"的标识，变了就无法把多个服务的日志串起来——这是 `traceparent` 存在的唯一理由。`span_id` 标识"这一跳"，每跳换新才能区分父子、算出每一跳的耗时。

</details>

<details>
<summary><b>Q2：畸形 `traceparent` 该报错还是忽略？</b></summary>

忽略（当没收到，自己起新链路）。规范明确要求宽容处理；如果抛异常，任何人用一个畸形 header 就能让你的服务 500——**观测设施不能变成攻击面**。

</details>

<details>
<summary><b>Q3：`flags=00`（不采样）时还要透传吗？</b></summary>

要。透传是义务，记录是选择。上游可能想看这条链路，只是这一跳不想付记录成本；如果你把 header 丢了，上游会看到一棵断掉的树。

</details>

### 9.2 动手任务：把 trace 接进 3.7 的复合图

**要求**

1. 给 `build_composite_graph` 的每个节点包一层 span（用本节的 `Tracer`）。
2. 让 `retrieval` 子图调 MCP Server 时**透传 traceparent**（复用 2.7 的 bridge）。
3. 跑一次完整工单，导出 span 并渲染成树。
4. 验证：树里能同时看到"图节点 span"与"mcp.handle span"，且 parent 链正确。

**验收标准**

- 树里 `mcp.handle` 的 parent 是某个图节点的 span（不是根）。
- 一条 trace 覆盖"客户端 → 图节点 → MCP Server"三个层次。
- 原有 502 个用例仍全绿。

**提示**：图节点是同步函数，span 用 `with` 包住即可；注意**异常路径也要记 span**
（本节的 `Tracer.span` 在 `finally` 里收口，已经处理了）。

### 9.3 拓展挑战

实现"日志 ↔ trace 双向定位"：已知一个 trace_id，列出该链路的所有日志行；
已知一行日志的关键词，找到它属于哪条 trace。

**思考**：

1. 日志里需要哪些字段才能双向查？（`trace_id` + `span_id` 够吗？）
2. 跨进程时日志在不同文件/系统里，怎么聚合？
3. 采样率低时，很多日志没有 trace——怎么处理这种"部分链路"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **`traceparent` 四字段**：`trace_id` 一路不变、`span_id` 每跳换新、`parent` 指回上一跳；**解析结果代表上游跨度**，本地跨度要 `.child()`。
2. **宽容失败**：畸形 header 丢弃重建，绝不 500；**采样=00 时透传但不记录**。
3. **实测跨三进程 + MCP**：一条 trace、正确父子关系、耗时可比（`client 297ms → A 149ms → B 0ms`）。

**已知限制（诚实记录）**

- **没有接 OpenTelemetry**：自己实现的极简 tracer，`tracestate` 只做了解析没落地。
- **没有真实后端**：span 落 JSONL 文件，不是 Jaeger/Tempo。
- **没有 W3C 一致性测试**：只覆盖了本文用到的规则。
- **MCP 改造是最小版**：只记 `mcp.handle`，没细分到每个工具调用。

**延伸阅读**

- W3C Trace Context 规范：https://www.w3.org/TR/trace-context/
- OpenTelemetry 语义约定：https://opentelemetry.io/docs/specs/semconv/
- Google Dapper 论文（分布式追踪的起点）：https://research.google/pubs/pub36356/

---

## 修订记录

- 2026-11-18 初稿：七步演示与 27 个测试全部实机运行；跨三进程与 MCP 调用的调用树均验证通过
- 2026-11-18 修正：`parse_traceparent` 初版在解析时就生成新 `span_id` 并把上游当 `parent_id`，导致 `.child()` 的 parent 指向一个从未被记录的 span，树还原成多个平级根；改为"解析结果代表上游跨度"
- 2026-11-18 修正：`trace_service` 的 `inject(headers, child)` 放的是 child 的 span_id，导致下游 parent 指向不存在的跨度；改为 `inject(headers, mine)`
- 2026-11-18 补充：给 `http_app._json()` 加 span 收口（所有 JSON 响应的唯一出口，含 4xx 提前返回）
