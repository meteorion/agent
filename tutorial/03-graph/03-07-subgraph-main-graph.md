---
section: "3.7"
title: "子图拆分与 M3 端到端验收"
stage: 3
status: done
depth: L2
difficulty: 4
minutes: 110
milestone: M3
depends_on: ["3.6"]
produces: ["graph/composite.py"]
verifies: "父图调用子图并正确合并 state"
part: null
updated: "2026-11-18"
---

# 3.7 子图拆分与 M3 端到端验收

## 0. 本节导航

**本节在链路中的位置**：阶段三收官。前六节造出了节点、状态、边、断点、审批、流式——但都在**一张图**里。
本节把它们拆成可复用的子图，拼成一条完整的工单链路，并做 **M3 验收**。

**本节你将获得**

1. 子图当节点的三种接法，以及"同 schema 时直接透传"的条件。
2. 子图里的 `interrupt` 会**冒泡**到父图（实测 `next=('approval',)`）。
3. 子图可**独立测试**——这是拆分最大的收益。
4. 一个**实测缺陷**：子图内 `interrupt` 之后恢复，父图会**重跑前置兄弟节点**；
   `durability=async/sync/exit` 都改不了。给了三种规避方案。
5. **M3 验收表**（7 项能力，全部有实测证据）。

**前置检查清单**

- 3.5 的 `uv run python -m scripts.approval_demo` 能跑通（本节复用 interrupt 语义）。
- 3.6 的 `uv run python -m scripts.streaming_demo` 能跑通。

**难度**：★★★★☆ ｜ **预计时长**：110 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

把 3.1→3.6 的东西堆进一张图，会得到什么？

- 节点数 15+，边数 20+
- 想单独验证"检索逻辑对不对"，只能**把整条链路跑一遍**（还要准备审批）
- 想复用"检索"给另一个流程，只能**复制粘贴**

**子图就是解决这三件事的。** 拆开之后：

```
父图：  review（2 个节点）
          ├── retrieval  ── classify → fetch → compose / ask_more
          └── approval   ── prepare → gate(interrupt) → deliver/cancelled/expired
```

- 检索子图能**单独跑、单独测**（不需要审批）
- 审批子图能**单独跑、单独测**（不需要检索）
- 两条链路都能被别的父图复用

**代价**：多一层嵌套，`state` 的流动变复杂——本节会实测出一个具体的坑。

---

## 2. 核心概念与架构设计

### 2.1 类比：乐高模块

- **子图 = 一个模块**（有标准接口，能单独玩，也能拼进大模型）
- **父图 = 组装说明书**（规定模块之间怎么接）
- **State = 模块之间流转的"公头"**（接口对得上才能直连，对不上要转接头）

> **这个类比在哪失效**：乐高模块拼好后是**静态**的；而子图里的 `interrupt` 会让
> 父图的执行**回退重来**。§6.1 会实测这个反直觉行为。

### 2.2 三种接法

| | 做法 | 何时用 |
|---|---|---|
| **① 子图当节点** | `builder.add_node("retrieval", subgraph)` | 同 schema、纯组合（**首选**） |
| **② 包装函数** | `async def node(s): return await subgraph.ainvoke(s)` | 要改名、裁剪字段、加前后逻辑 |
| **③ schema 映射** | 编译时声明 input/output schema | 字段名差异大、又不想写包装函数 |

**本节实现 ①②，③ 只说明。** 关键约束：

> **方式 ① 要求父子图共享同一个 State schema**（否则字段对不上）。
> 本节的 `TicketFlowState` 就是父子共用的。

### 2.3 子图的 `interrupt` 会冒泡

实测：

```
挂起了吗: True
next    : ('approval',)   ← 注意是父图的 approval 节点
审批单  : ticket=T-1001 risk=high
```

**父图看到的是"approval 这个节点还没跑完"**，而不是"子图内部某个节点挂了"。
`Command(resume=...)` 也能正确地把答案送进子图的 `gate` 节点。

**这个能力是"能拆图"的前提**——否则审批子图没法嵌进任何流程。

### 2.4 展开视图（xray）

```python
graph.get_graph()             # 父图：2 个节点
graph.get_graph(xray=True)    # 展开：12 个节点，名字带子图前缀
```

实测展开后的节点名：

```
retrieval:classify, retrieval:fetch, retrieval:compose, retrieval:ask_more,
approval:prepare_approval, approval:gate, approval:deliver, …
```

**写设计文档、做排障时用 `xray=True`**——否则你只看到两个黑盒。

### 2.5 适用范围（边界）

| 场景 | 建议 |
|---|---|
| 逻辑能独立测试 | ✅ 拆 |
| 会被多个父图复用 | ✅ 拆 |
| 子图里要 `interrupt` | ⚠️ 可拆，但要接受 §6.1 的重跑 |
| 只有 2–3 个节点 | ❌ 别拆，嵌套成本大于收益 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 |
|---|---|
| langgraph | `1.2.22` |
| langgraph-checkpoint-sqlite | `3.1.3` |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from servicedesk.graph.composite import build_composite_graph; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/composite.py` | **主角**：两个子图 + 两种合成方式 |
| `scripts/composite_demo.py` | 七步演示 + M3 验收表 |
| `tests/test_graph_composite.py` | 12 个断言 |

### 4.1 共享 State

```python
class TicketFlowState(TypedDict, total=False):
    # 输入
    ticket_id: str
    title: str
    description: str
    # 检索子图产出
    intent: str
    docs: list[str]
    draft: str
    error: str | None
    # 审批子图产出
    risk: str
    expires_at: float
    decision: str
    final: str
    # 观测
    trace: Annotated[list[str], operator.add]
    stages: Annotated[list[str], operator.add]     # 走过的子图
```

### 4.2 子图当节点

```python
def build_composite_graph(checkpointer: Any = None):
    builder = StateGraph(TicketFlowState)
    builder.add_node("retrieval", build_retrieval_subgraph())
    builder.add_node("approval", build_approval_subgraph())

    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=checkpointer)
```

**就这样——没有 state 映射、没有额外参数。** 前提是两个子图用同一个 schema。

### 4.3 包装函数（只在需要时用）

```python
    async def retrieval_node(state: TicketFlowState) -> dict[str, Any]:
        result = await retrieval.ainvoke(state)
        return {"intent": result.get("intent"), "docs": result.get("docs"),
                "draft": result.get("draft"), "error": result.get("error"),
                "trace": ["retrieval(callable)"], "stages": ["retrieval"]}
```

**注意 `trace` 被我压成了一行**——这是包装函数的可观测代价（§6.2）。

### 4.4 运行

```powershell
uv run python -m scripts.composite_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么"同 schema"是硬约束

方式 ① 是**直接透传**：父图的 state 原样喂给子图，子图的返回原样合并回父图。
两边字段名不同，就会"写进去了但读不出来"。

**所以拆图的第一步不是画图，是设计 schema。** 本节的 `TicketFlowState` 是父子共用的一份。

### 5.2 子图独立运行时 `stages` 只记自己

实测：

```
子图 A（检索）单独跑：
  trace=['classify', 'fetch', 'draft']  stages=['retrieval']
```

`stages` 由 `classify` 写入 —— 说明**子图自报家门**，父图不用猜。

**这个设计让"这条链路经过了哪些子系统"变成可查询的数据**，而不需要读代码。

### 5.3 没有草稿时不该挂起

`approval` 子图的 `gate` 节点开头有一句：

```python
    if not state.get("draft"):
        return {"decision": "skipped", "trace": ["gate:skipped"]}
```

**没有可审批的东西，就不该让人来点。** 实测（第 4 步）：

```
no-docs    未挂起 → final: 资料不足，已请用户补充信息。
```

**这是一个容易漏的分支**：很多人只在"正常路径"上加人工门禁，忘了"异常路径"也会走到门禁。

### 5.4 `interrupt` 在子图里，`resume` 从哪进

从父图进：

```python
await graph.ainvoke(Command(resume="approve"), config=config)
```

LangGraph 会把答案送到**子图里那个正在挂起的 `interrupt`**。
你不需要（也无法）直接对子图 `resume`——**子图不是独立会话，它是父图的一部分。**

### 5.5 为什么 `stages` 会出现两次 `retrieval`

这是本节最重要的实测发现，见 §6.1。

### 5.6 `get_graph(xray=True)` 的用途

排障时你要回答"这个请求经过了哪些节点"。展开后能直接对上：

```
retrieval:classify → retrieval:fetch → retrieval:compose
```

**注意前缀**：`子图名:节点名`。写正则过滤时别把 `:` 当成分隔符漏了。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：子图内 `interrupt` → 父图重跑前置节点（**实测缺陷**）

实测（第 3 步，批准后）：

```
stages  : ['retrieval', 'retrieval', 'approval']
trace   : ['classify', 'fetch', 'draft', 'classify', 'fetch', 'draft',
           'prepare_approval', 'gate:approve', 'deliver']
```

**`retrieval` 子图整个跑了两遍。**

我做了三件事确认这不是 demo 写错：

1. 看挂起时的快照：`next=('approval',)`，`trace` 只有一份 —— 父图"认为"检索已完成
2. 看历史：`next=('approval',)` 之前的快照是 `next=('retrieval',)`
3. 试了 `durability` 三档：

```
durability='async'  stages=['retrieval', 'retrieval', 'approval']  trace_len=9
durability='sync'   stages=['retrieval', 'retrieval', 'approval']  trace_len=9
durability='exit'   stages=['retrieval', 'retrieval', 'approval']  trace_len=9
```

**都重跑。** 所以这是本版本 LangGraph 在"嵌套子图 + interrupt"下的行为：
恢复时父图回退到上一个**稳定**检查点（`next=('retrieval',)`）重放。

**好在"出口节点"不重复**（实测 `deliver` 只出现 1 次），所以**用户不会收到两条交付**。

**三种规避方案**：

| 方案 | 做法 | 代价 |
|---|---|---|
| ① 前置纯读 | 让 `retrieval` 只读、可重跑 | 重复的计算/检索开销 |
| ② 副作用后置 | 把发通知/扣款放到 `deliver` 之后 | 业务上不一定允许 |
| ③ **不嵌子图** | 把 `interrupt` 放在父图（3.5 就是平的） | 图会变长，失去复用 |

**本节的 demo 之所以还能跑，是因为 `retrieval` 是纯读的（查内置字典）。**
换成真实检索，这就是"一次审批 = 两次向量检索 = 两份账单"。

> 这条我**没有找到官方文档明确说明**，是实测出来的（⚠️ 待核实：是否为已知问题/已在更高版本修复）。
> `tests/test_graph_composite.py` 里有一条断言把当前行为固定住——**哪天它失败了，说明修好了，记得回来改这一节**。

### 6.2 实验 B：包装函数里调带 `interrupt` 的子图 → 无法恢复

我最初把 `approval` 也用包装函数接：

```python
    async def approval_node(state):
        result = await approval.ainvoke(state)      # ← 另起一次执行
        return {"decision": result.get("decision"), ...}
```

结果 `pending_interrupt()` 直接炸：父图没有 checkpointer 上下文可读。

**原因**：包装函数里的 `ainvoke` 是**一次独立的子执行**，父图的 `config`（含 checkpointer）
没有传进去，所以挂起信息落不到父图的 store 里。

**结论**：**带 `interrupt` 的子图必须直接当节点用**（方式 ①）。
包装函数（方式 ②）只适合无 interrupt 的纯计算子图。

### 6.3 实验 C：包装函数让 trace 变薄

对比两条 trace：

```
直接当节点：['classify', 'fetch', 'draft', 'prepare_approval', 'gate:approve', 'deliver']
包装函数  ：['retrieval(callable)', 'prepare_approval', 'gate:approve', 'deliver']
```

**包装函数里子图内部的 `classify/fetch/draft` 全没了**，只剩一行。

**这就是"省事"与"可观测"的取舍**：想保留内部轨迹，就别包；包了就要自己在包装层补日志。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.composite_demo
```

实测（节选）：

```
第 1 步：两个可以单独跑的子图
  子图 A（检索）单独跑：
    intent=vpn  draft=根据资料：认证失败通常有三类原因…
    trace=['classify', 'fetch', 'draft']  stages=['retrieval']
  子图 B（审批）没有草稿时会短路（不挂起）：
    decision=skipped  final=资料不足，已请用户补充信息。

第 2 步：父图 = 检索子图 → 审批子图
  父图节点: ['retrieval', 'approval']
  父图边  : [('retrieval', 'approval')]
  展开后节点数: 12  ['retrieval:classify', 'retrieval:fetch', …]

第 3 步：端到端
  ① 发起：
     挂起了吗: True
     next    : ('approval',)   ← 注意是父图的 approval 节点
     审批单  : ticket=T-1001 risk=high
  ② 人类批准后恢复：
     decision=approve
     final   : 已交付：根据资料：认证失败通常有三类原因…
     stages  : ['retrieval', 'retrieval', 'approval']

  ⚠️ 注意：retrieval 被跑了 2 次。
     这不是 demo 写错，是本版本 LangGraph 的行为…

第 4 步：资料的四种结局
  approve    approve  → 已交付：根据资料：…
  reject     reject   → 审批未通过，已转人工。
  no-docs    未挂起 → final: 资料不足，已请用户补充信息。
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 77%]
..............................................................           [100%]
278 passed in 33.39s
```

---

## 8. M3 里程碑验收

| # | 验收项 | 证据 | 状态 |
|---|---|---|---|
| 1 | 状态图替代 if-else | 3.1 三节点图 + `CompiledStateGraph` | ✅ |
| 2 | State 与 reducer | 3.2 `messages` 保留历史、派生字段现算 | ✅ |
| 3 | 条件边与循环 | 3.3 三路分支 + 重试 + 兜底 + `max_attempts` 刹车 | ✅ |
| 4 | 断点续跑 | 3.4 真杀进程（`os._exit`）后从 step2 继续 | ✅ |
| 5 | 人工审批 | 3.5 挂起 → 进程退出 → 新进程恢复 → 交付 | ✅ |
| 6 | 流式与可观测 | 3.6 15s 任务首条进度 0.00s | ✅ |
| 7 | **子图拆分 + 端到端** | 本轮：父图调两个子图，interrupt 冒泡，四种结局正确 | ✅ |

**M3 完成标志**：一条工单流程能分叉、能重试、能断点续跑、能等人审批，
并且用户全程看得到进度——**且整条链路由可复用、可单测的子图组成**。

**M3 遗留问题（带进阶段四）**

1. **子图嵌套重跑**（§6.1）：前置节点必须可重跑。
2. **没有真实 LLM**：本阶段全部用确定性逻辑，模型能力要等阶段五评估。
3. **审批权限未校验**：任何能调 `resume_flow` 的人都能批（2.6 有 `admin:approve`，没接上）。
4. **没有多 Agent**：这条链路还是单线程的——阶段四要解决"多角色协作"。

---

## 9. 生产环境踩坑实录

### 坑 1：包装函数里放 `interrupt`，挂起后无法恢复

**现象**

```
await pending_interrupt(graph, thread_id)   # 报错 / 返回 None
```

**根因**

```python
async def approval_node(state):
    return await approval_subgraph.ainvoke(state)   # ← 另起一次执行
```

父图的 checkpointer 上下文没传进去。

**修法**

带 `interrupt` 的子图**直接当节点**：

```diff
- builder.add_node("approval", approval_node)
+ builder.add_node("approval", build_approval_subgraph())
```

**顺手加固**：写一条测试断言"挂起后 `next` 指向该子图节点"，把这类回归挡住。

### 坑 2：父子图 schema 不一致，字段"写了但读不到"

**现象**

子图里明明 `return {"result": ...}`，父图下一个节点读 `state["result"]` 是 `None`。

**根因**

父图 schema 里没有 `result` 键 → 合并时被丢弃（有些版本会静默忽略）。

**修法**

把字段加到共享 schema，或用方式 ③ 做显式映射。

**顺手加固**：给共享 schema 写一条测试，列出所有键并断言"每个键都有节点写它"（3.2 的
`describe_state_schema` 就是雏形）。

### 坑 3：以为嵌套子图不会重复执行前置节点

**现象**

审批通过后，日志里出现两条"检索完成"；向量库 QPS 翻倍。

**定位方法**

在 `stages` / `trace` 里数重复：

```python
from collections import Counter
print(Counter(out["stages"]))
# Counter({'retrieval': 2, 'approval': 1})
```

**修法**

见 §6.1 的三种规避；至少要让前置节点**纯读**。

**顺手加固**：给前置节点的外部调用加缓存（按 `thread_id + 节点名` 命中），
重跑时直接返回上次结果。

---

## 10. 自检与作业

### 10.1 三个自检问题

<details>
<summary><b>Q1：什么时候该把图拆成子图？</b></summary>

两个判据：① 这段逻辑能独立测试吗（不需要准备整条链路）？② 它会被多个父图复用吗？两条都"是"才拆。只有 2–3 个节点、且只在一个流程里用，拆了只会增加嵌套成本。

</details>

<details>
<summary><b>Q2：子图里的 `interrupt` 怎么冒泡到父图？要注意什么？</b></summary>

子图作为节点时，LangGraph 会把内部的挂起上报为"该父节点未完成"，`next` 指向父图的这个子图节点，`Command(resume=...)` 也能送进子图的 `interrupt`。**注意**：不能用包装函数接带 `interrupt` 的子图——那会另起一次执行，丢失父图的 checkpointer 上下文。

</details>

<details>
<summary><b>Q3：为什么"前置子图被重跑"这件事必须写进文档？</b></summary>

因为它不报错、不影响正确性（出口只执行一次），但会**让前置的计算/检索/计费翻倍**。读者如果不知道，会在生产里看到"一次审批两次检索"的账单却找不到原因。而且这个行为是**版本相关的**——所以我用测试把它固定住，未来修好了会失败提醒。

</details>

### 10.2 动手任务：把父图改成"平的"

**要求**

1. 复制 `build_composite_graph` 的逻辑，改成**不嵌子图**：把所有节点放在同一层
   （`classify → fetch → compose → prepare_approval → gate → deliver`）。
2. 用同一个测试思路验证：批准后 `trace` 里 `classify` 只出现 **1 次**。
3. 对比两种结构的代码行数、可复用性、`trace` 完整度。
4. 写一段 200 字的结论：你的业务该选哪种。

**验收标准**

- 平图的 `trace` 无重复（`classify` 计数 == 1）。
- 平图仍然能挂起/恢复（`next` 指向 `gate`）。
- 两种结构都能通过"四种结局"的用例。
- 原有 278 个用例仍全绿。

**提示**：平图的代价是"检索逻辑不能直接复用给别的流程"——**这就是取舍本身**。

### 10.3 拓展挑战

给 `retrieval` 子图加"重跑缓存"：按 `thread_id + 节点名 + 输入哈希` 缓存结果，重跑时命中缓存。

**思考**：

1. 缓存 key 里为什么必须包含**输入哈希**？（提示：同一 thread 的输入可能变）
2. 缓存放哪？（内存 / SQLite / Redis）各自的代价是什么？
3. 如果检索有"实时性"要求，缓存多久算过期？

---

## 11. 本节小结与延伸

**三点回顾**

1. **同 schema 时，编译好的子图直接当节点**，state 自动透传，`interrupt` 自动冒泡；需要映射或加逻辑时才用包装函数。
2. **子图能单独跑、单独测**——这是拆分的最大收益（第 1 步：两个子图各自独立工作）。
3. **实测缺陷**：子图内 `interrupt` 后恢复，父图会**重跑前置兄弟节点**（`retrieval` ×2），`durability` 三档都改不了。规避：前置纯读 / 副作用后置 / 不嵌子图。

**已知限制（诚实记录）**

- **子图重跑未解决**：只有实测结论与三种规避，没有根治（⚠️ 待核实是否为已知 issue）。
- **方式 ③ 未实现**：schema 映射只做了说明。
- **无 LLM**：整条链路是确定性逻辑。
- **审批无权限校验**：`admin:approve` 没接上。

**延伸阅读**

- LangGraph 子图：https://langchain-ai.github.io/langgraph/concepts/low_level/#subgraphs
- 嵌套图的限制：https://langchain-ai.github.io/langgraph/troubleshooting/errors/
- `get_graph(xray=True)`：https://langchain-ai.github.io/langgraph/how-tos/visualization/

---

## 修订记录

- 2026-11-18 初稿：七步演示与 12 个测试全部实机运行；M3 验收表完成
- 2026-11-18 发现并记录：子图内 `interrupt` 恢复后父图重跑前置子图（`stages=['retrieval','retrieval','approval']`）；`durability=async/sync/exit` 均无效；已加测试固定行为并写入 §6.1
- 2026-11-18 修正：`build_composite_via_callable` 最初把带 `interrupt` 的审批子图也用包装函数接，导致挂起后无法恢复；改为审批子图直接当节点
