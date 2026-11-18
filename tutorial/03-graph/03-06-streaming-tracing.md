---
section: "3.6"
title: "流式输出与可观测：把「图在干嘛」讲给用户"
stage: 3
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M3
depends_on: ["3.5"]
produces: ["graph/streaming.py"]
verifies: "演示一个 15 秒的任务，用户在前 2 秒就能看到进度"
part: null
updated: "2026-11-18"
---

# 3.6 流式输出与可观测：把「图在干嘛」讲给用户

## 0. 本节导航

**本节在链路中的位置**：3.5 之后，图会挂起等人了。但用户端看到的是"转圈 15 秒"——他既不知道卡在哪，也不知道还要等多久。本节解决**感知延迟**，并为后面（阶段五的可观测）打底。

**本节你将获得**

1. `astream(stream_mode="updates")` 与 `astream_events(version="v2")` 的分工。
2. **实测对比**：15 秒任务，阻塞版第一次出结果在 15.0s；流式版第一条进度在 **0.00s**。
3. 一个"扁平事件"包装层——前端不必理解 LangGraph 的内部结构。
4. `trace_id` 通过 `contextvars` 贯穿日志与事件，一次请求一条线。
5. 一个容易忽略的坑：**同步节点会阻塞事件循环**，流式就"不流"了。

**前置检查清单**

- 3.5 的 `uv run python -m scripts.approval_demo` 能跑通。
- 知道异步生成器（`async for`）的用法。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

本节实测（5 个节点，每个 3 秒）：

```
第 1 步：阻塞调用
  第一次看到输出: 15.0s（就在全部跑完之后）
  >>> 用户盯着转圈 15 秒。他甚至不知道卡在哪一步。
```

**用户抱怨的往往不是"慢"，而是"不知道还要等多久"。**

这句话在客服系统里几乎是定律。15 秒的等待，如果配上"正在检索知识库…（第 2/5 步）"，
体感完全不同——因为**不确定性被消除了**。

而可观测还有第二个收益：**排障**。当用户说"卡住了"，你要能回答"卡在第几步、当时在干什么"。
如果只有一个 `ainvoke`，你只能看到"15 秒后返回"。

---

## 2. 核心概念与架构设计

### 2.1 类比：快递物流轨迹

- **阻塞调用** = 只有一个状态："已发货"。货到之前你什么都不知道。
- **`astream(updates)`** = 节点级轨迹："已揽收 → 运输中 → 派送中 → 已签收"
- **`astream_events(v2)`** = 细到每个动作："快递员 09:12 到了 A 小区"

> **这个类比在哪失效**：物流轨迹是**事后**记录的，而流式事件是**实时**推送的——
> 事件到达时这件事往往还没做完（`node_start` 先到，`node_end` 后到）。

### 2.2 两种流，用途不同

| | `astream(stream_mode="updates")` | `astream_events(version="v2")` |
|---|---|---|
| 粒度 | 每个节点跑完后 **1 条** | 每个节点 **2+ 条**（开始/结束），工具另有事件 |
| 形状 | `{"节点名": {增量}}` | `{"event": "on_chain_start", "name": ..., "data": ...}` |
| 适合 | 画进度条、更新状态 | 单独计时、展示工具调用、token 流 |
| 复杂度 | 低 | 中（10+ 种 `on_*` 类型） |

**本节两种都用**：先用 `astream_events` 做成细粒度进度，再用 `astream` 做最简单的节点级流。

### 2.3 为什么要"再包一层"

LangGraph 的原始事件长这样（实测 2 个节点产生 **10 条**事件）：

```
on_chain_start   name='LangGraph'   data=['input']
on_chain_start   name='a'           data=['input']
on_chain_stream  name='a'           data=['chunk']
on_chain_end     name='a'           data=['output', 'input']
...
```

**把这些直接丢给前端是灾难**：前端要懂 `on_chain_*`、要过滤 `LangGraph` 这类内部节点、
要自己算相对时间。

所以本节定义了 `ProgressEvent`：

```python
@dataclass(frozen=True)
class ProgressEvent:
    at: float        # 相对开始的秒数
    kind: str        # graph_start / node_start / node_end / graph_end
    node: str
    detail: str      # 人话
    trace_id: str
```

**四种 kind，五个字段。** 前端只认这个。

### 2.4 `trace_id` 怎么贯穿

用 `contextvars`——`bind_request_context(trace_id=...)` 之后，**这个请求里所有 structlog 日志
都会自动带上它**（1.x 就已实现，本节第一次真正用起来）：

```
[info] node_start  at=0.0 node=classify trace_id=tr_f30a4c83195a4e2f
[info] node_end    at=0.07 node=classify trace_id=tr_f30a4c83195a4e2f
```

**注意用 `finally: clear_request_context()`**——否则下一个请求会继承上一个的 trace_id，
日志串成一锅粥。

### 2.5 适用范围（边界）

| 场景 | 做法 |
|---|---|
| Web 界面展示进度 | ✅ 流式（SSE / WebSocket 推 `ProgressEvent`） |
| 后台批处理 | ❌ 不需要，写日志就够 |
| 单步任务（< 2 秒） | ⚠️ 收益有限，别过度设计 |
| 要给用户看 token 级生成 | ✅ `astream_events` 的 `on_chat_model_stream` |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| langgraph | `1.2.22` | `astream` / `astream_events` |
| structlog | `26.1.0` | `contextvars` 绑定 trace_id |
| Python | `3.12.13` | `AsyncIterator` |

**验证**

```powershell
uv run python -c "from servicedesk.graph.streaming import build_pipeline_graph; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/streaming.py` | **主角**：事件模型 + 两种流 + 渲染 |
| `scripts/streaming_demo.py` | 五步演示（含 15 秒对比） |
| `tests/test_graph_streaming.py` | 10 个断言 |

### 4.1 演示图（5 步 × 3 秒）

```python
def build_pipeline_graph(*, step_seconds: float = 3.0):
    def make_node(name: str):
        async def node(state: PipelineState) -> dict[str, Any]:
            await asyncio.sleep(step_seconds)   # 模拟真实耗时（检索 / LLM 生成）
            return {"stage": name, "trace": [*(state.get("trace") or []), name], ...}
        return node
    ...
```

**注意是 `async def` + `await asyncio.sleep`**，不是 `time.sleep`（§8.1 讲为什么）。

### 4.2 核心：把原始事件翻译成进度

```python
async def stream_progress(graph, payload, *, config=None, trace_id=None):
    trace_id = trace_id or new_trace_id()
    bind_request_context(trace_id=trace_id)
    started = time.perf_counter()

    try:
        yield ProgressEvent(0.0, "graph_start", "", "开始处理", trace_id)

        async for event in graph.astream_events(payload, config=config, version="v2"):
            kind = event.get("event")
            name = str(event.get("name") or "")
            if name in ("LangGraph", "__start__", "__end__"):
                continue          # 过滤内部节点

            at = time.perf_counter() - started
            if kind == "on_chain_start":
                yield ProgressEvent(at, "node_start", name, f"{NODE_LABELS.get(name, name)}…", trace_id)
            elif kind == "on_chain_end":
                yield ProgressEvent(at, "node_end", name, f"{NODE_LABELS.get(name, name)} ✓", trace_id)

        total = time.perf_counter() - started
        yield ProgressEvent(total, "graph_end", "", f"处理完成，用时 {total:.1f}s", trace_id)
    finally:
        clear_request_context()
```

### 4.3 节点名 → 人话

```python
NODE_LABELS = {
    "classify": "识别问题类型",
    "retrieve": "检索知识库",
    "rerank":   "重排候选段落",
    "draft":    "生成答复草稿",
    "finalize": "校对并定稿",
}
```

**内部节点名不该出现在用户界面上**（`retrieve_v2_tmp` 这种名字会让用户困惑）。

### 4.4 运行

```powershell
uv run python -m scripts.streaming_demo                       # 每步 3 秒，全程 15 秒
$env:STEP_SECONDS="0.2"; uv run python -m scripts.streaming_demo   # 快速跑
```

---

## 5. 逐块拆解

### 5.1 `at` 为什么用 `perf_counter` 而不是 `time.time`

`time.time()` 是墙上时钟，会被 NTP 校时、夏令时影响——**可能倒退**。
相对耗时必须用单调时钟（`perf_counter`）。

**墙上时钟用于"事件发生的绝对时间"（写日志），单调时钟用于"耗时计算"。**

### 5.2 为什么事件要带 `trace_id`

因为前端拿到事件后，可能只把其中一条截图给技术支持。
**带上 trace_id，就能从这一条找到整条链路的所有日志。**

这也是本节的 `render_progress` 之外，`ProgressEvent` 仍然保留该字段的原因——
**给人看的是 `detail`，给机器查的是 `trace_id`。**

### 5.3 `finally: clear_request_context()`

`contextvars` 是**当前协程上下文**的变量。忘了清，后果有两种：

- 同一进程内的下一个请求继承了上一个的 trace_id → 日志混杂
- 长跑的服务内存里堆积旧的上下文 → 泄漏

**规则：`bind` 之后必须有 `finally: clear`。**

### 5.4 为什么 `stream_node_updates` 还要留着

因为**大部分场景不需要细粒度**。画一个 5 步进度条，`updates` 模式一行代码就够了：

```python
async for node, update in stream_node_updates(graph, {"trace": []}):
    ...
```

**别为了"用上高级 API"而增加复杂度。** 需要"单独给工具调用计时"时，再换 `astream_events`。

### 5.5 `graph_start` / `graph_end` 为什么也要发

因为前端需要一个**明确的开始与结束**信号：

- `graph_start` → 显示"处理中"，禁用提交按钮
- `graph_end` → 隐藏进度条，显示结果

否则前端只能靠"多久没收到事件"猜结束——这在长任务里必然猜错。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把节点写成同步 `time.sleep`

我在第一版里把节点写成：

```python
def node(state):
    time.sleep(step_seconds)      # ← 同步阻塞
    return {...}
```

在**异步执行**下，同步节点会被放到线程池里跑，看起来能工作；
但如果节点里有阻塞 I/O（同步 HTTP、数据库驱动），线程池会被占满，
**其它请求一起卡住**，流式事件也会延迟到达。

**修法**：节点用 `async def` + `await asyncio.sleep`（或 `await` 异步客户端）。

**判据**：这个节点会阻塞多久？超过几十毫秒且能异步化，就必须异步。

### 6.2 实验 B：不过滤 `LangGraph` 内部事件

不过滤的话，前端会收到这些噪声：

```
graph_start
  → on_chain_start name='LangGraph'
  → on_chain_start name='__start__'
  → on_chain_start name='classify'
  ...
```

用户会看到进度条突然出现"LangGraph"和"__start__"两个步骤。

**修法**：`if name in ("LangGraph", "__start__", "__end__"): continue`。

### 6.3 实验 C：不绑 trace_id

不绑的话，日志长这样：

```
[info] node_start at=0.0 node=classify
[info] node_start at=0.0 node=classify      ← 另一个并发请求
```

**两条请求的日志交错，无法区分。** 绑上之后：

```
[info] node_start ... trace_id=tr_f30a4c83195a4e2f
[info] node_start ... trace_id=tr_9b21e7c40d1a55f8
```

**一眼就能按 trace_id 过滤出一次请求的全貌。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.streaming_demo
```

实测（每步 3 秒，全程 15 秒）：

```
每步耗时: 3.0s，共 5 步

第 1 步：阻塞调用 —— 15 秒里用户什么都看不到
  第一次看到输出: 15.0s（就在全部跑完之后）
  结果: stage=finalize answer=已完成的答复
  >>> 用户盯着转圈 15 秒。他甚至不知道卡在哪一步。

第 2 步：流式调用 —— 第一条事件在毫秒级到达
  trace_id: tr_6ca77fe3059f464d

    0.0s  graph_start              开始处理
    0.0s  node_start  识别问题类型    识别问题类型…  ◄ 用户第一次看到进度
    3.0s  node_end    识别问题类型    识别问题类型 ✓
    3.0s  node_start  检索知识库     检索知识库…
    6.0s  node_end    检索知识库     检索知识库 ✓
    9.0s  node_end    重排候选段落    重排候选段落 ✓
   12.0s  node_end    生成答复草稿    生成答复草稿 ✓
   15.0s  node_end    校对并定稿     校对并定稿 ✓
   15.0s  graph_end                处理完成，用时 15.0s

  首条进度: 0.00s    全部完成: 15.0s（与阻塞版相同）
  >>> 总耗时没变，但**用户从第 0.0 秒起就知道在干什么了**。

第 3 步：更简单的流 —— astream(stream_mode=updates)
    classify   → 更新字段: stage
    retrieve   → 更新字段: stage
    rerank     → 更新字段: stage
    draft      → 更新字段: stage
    finalize   → 更新字段: stage, answer

第 4 步：渲染成给用户看的进度条文字
  [  0.0s] 开始处理
  [  0.6s] 第 1/5 步：识别问题类型 ✓
  [  1.2s] 第 2/5 步：检索知识库 ✓
  ...
  [  3.1s] 处理完成，用时 3.1s

第 5 步：两种调用的体验对比
  阻塞版：用户可见进度点：1 个（在最后）
  流式版：用户可见进度点：7 个
```

**本节验收点**：15 秒的任务，**首条进度在 0.00s**（远早于 2 秒），
并且第 2 秒时用户已经看到"识别问题类型"完成。

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 81%]
..................................................                       [100%]
266 passed in 33.15s
```

---

## 8. 生产环境踩坑实录

### 坑 1：节点里用同步 I/O，流式"不流"了

**现象**

进度条不更新，直到全部结束才一次性刷出 7 条事件。

**定位方法**

看事件到达时间是否全部集中在最后：

```python
log.info("event_at", kind=event.kind, at=round(at, 2))
```

如果全在 15.0s 附近，就是这个坑。

**根因**

节点是同步函数、内部用 `time.sleep` 或同步 HTTP 客户端，占满了线程池，
事件循环没机会把事件推出去。

**修法**

节点改 `async def`，I/O 改 `await`。

**顺手加固**：CI 里加一条"首条事件延迟 < 500ms"的断言（本节的
`test_first_event_arrives_before_work_finishes` 就是雏形）。

### 坑 2：忘了 `clear_request_context()`

**现象**

并发请求的日志互相串：A 请求的日志里出现了 B 的 trace_id。

**根因**

`bind_request_context` 绑在上下文里，请求结束没清。

**修法**

```python
try:
    bind_request_context(trace_id=...)
    ...
finally:
    clear_request_context()
```

**顺手加固**：把"绑上下文"做成中间件/装饰器，而不是散落在业务代码里。

### 坑 3：把 LangGraph 内部节点名直接暴露给用户

**现象**

用户看到进度条里有"LangGraph"和"__start__"。

**根因**

没过滤 `__start__` / `__end__` / `LangGraph`。

**修法**

白名单式过滤 + `NODE_LABELS` 映射。

**顺手加固**：给 `NODE_LABELS` 加一条测试，保证每个节点都有中文标签（本节
`test_every_pipeline_node_has_a_label` 就是）。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：`astream(updates)` 和 `astream_events(v2)` 该怎么选？</b></summary>

只画进度条、更新界面状态 → `updates`，一行代码就够。需要"给工具调用单独计时""展示 token 流""区分节点的开始与结束"→ `astream_events`。别为了用高级 API 而增加复杂度。

</details>

<details>
<summary><b>Q2：为什么事件要再包一层，不直接推 LangGraph 的原始事件？</b></summary>

原始事件有 10+ 种 `on_*` 类型、会混入 `LangGraph`/`__start__` 等内部节点、时间戳是绝对时钟。前端被迫理解框架内部结构，改版本就崩。包成 4 种 kind + 5 个字段的扁平事件，前端与框架解耦。

</details>

<details>
<summary><b>Q3：`trace_id` 为什么放在 `contextvars` 里，而不是层层传参？</b></summary>

因为日志点分布在各层（节点、工具、HTTP 客户端），层层传参会污染所有签名。`contextvars` 是"当前协程的隐式上下文"，一次绑定、处处可见。**代价是必须显式清理**，否则会串到下一个请求。

</details>

### 9.2 动手任务：把流式接到真实 HTTP 上

**要求**

1. 用 `Streamable HTTP` 的 SSE（2.5 已经会了）把 `ProgressEvent` 推给客户端。
2. 客户端断开时，图要能**中止**（提示：`astream` 生成器被 `aclose()` 时会触发 `GeneratorExit`）。
3. 加一个"心跳"：超过 10 秒没有事件就发一条 `keepalive`，防止代理超时断连。
4. 补 3 个用例：正常推完、客户端提前断开、心跳出现。

**验收标准**

- 客户端能按到达顺序收到 7 条事件。
- 断开后图不再继续跑（用计数器验证）。
- 心跳不影响事件顺序。

**提示**：这是 2.5 的 SSE 与 3.6 的事件模型合流的地方——**协议层推的是 JSON，编排层产的是事件**，
中间那层转换就是你的活。

### 9.3 拓展挑战

把 `ProgressEvent` 落成"可观测数据"：每个节点的耗时、失败率、P95。

**思考**：

1. 事件里还需要补什么字段才能算 P95？（提示：`node` + `at` + `kind`）
2. 流式事件丢了一条，统计会怎样偏？（提示：`node_start` 有、`node_end` 没有）
3. 要不要给每条事件加 `seq`？（提示：2.5 的"断流没有续传"）

---

## 10. 本节小结与延伸

**三点回顾**

1. **15 秒任务，首条进度 0.00s**：总耗时不变，但用户从第一毫秒就知道在干什么。**抱怨"慢"的人，多半在抱怨"不确定"。**
2. **两种流分工**：`updates` 画进度条，`events` 做细粒度计时；两者都包装成扁平的 `ProgressEvent`，前端不碰框架内部结构。
3. **`trace_id` 走 `contextvars`**：一次绑定、处处可见，但**必须 `finally: clear`**。

**已知限制（诚实记录）**

- **没有接真实 HTTP 推送**：本节只在终端打印，SSE/WebSocket 是 §9.2 的作业。
- **没有 token 级流**：`on_chat_model_stream` 未演示（本节的节点不调 LLM）。
- **没有心跳与断连处理**：长任务下代理可能 60 秒断连。
- **`at` 是相对时间**：没写绝对时间戳，跨服务对齐需要额外字段。

**延伸阅读**

- LangGraph 流式：https://langchain-ai.github.io/langgraph/concepts/streaming/
- `astream_events` API：https://langchain-ai.github.io/langgraph/reference/graphs/#langgraph.graph.state.CompiledStateGraph.astream_events
- structlog `contextvars`：https://www.structlog.org/en/stable/contextvars.html

---

## 修订记录

- 2026-11-18 初稿：五步演示与 10 个测试全部实机运行；15 秒任务的首条进度实测 0.00s
- 2026-11-18 修正：节点最初用同步 `time.sleep`，会让事件攒到最后一次性刷出；改为 `async def` + `await asyncio.sleep`
- 2026-11-18 修正：`node_end` 的 detail 原本输出"更新：stage"这类内部字段名，改为 `NODE_LABELS` 的中文标签
- 2026-11-18 补充：过滤 `LangGraph` / `__start__` / `__end__` 内部节点，避免泄漏到用户界面
