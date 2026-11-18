---
section: "3.1"
title: "从「一长串 if」到状态图：为什么需要 LangGraph"
stage: 3
status: done
depth: L2
difficulty: 2
minutes: 75
milestone: M3
depends_on: ["2.7"]
produces: ["graph/minimal.py"]
verifies: "能画出自己业务的一张状态图并标出所有分支"
part: null
updated: "2026-11-18"
---

# 3.1 从「一长串 if」到状态图：为什么需要 LangGraph

> **版本提示**：本节实测用的是 **`langgraph 1.2.22`**（`langgraph-checkpoint 4.3.2`）。
> 教程大纲里曾按 `>=0.6,<0.7` 规划，实际生态已经走到 1.x。**API 形状未变**
> （`StateGraph` / `add_node` / `add_edge` / `START` / `END` / `compile` / `get_graph`），
> 但引用版本时请以你 `uv pip show langgraph` 的结果为准。

## 0. 本节导航

**本节在链路中的位置**：阶段三的开篇。2.7 的 Agent 已经能调工具，但它的"流程"仍然写在一段
`for round in range(...)` 里。本节要回答：**为什么这套控制流必须换成图**。3.2 讲 State 设计，
3.3 讲分支，3.4 讲断点，3.5 讲审批——全都建立在本节这张最小图之上。

**本节你将获得**

1. 手写 if-else 编排的**三种崩法**，每种都有实测症状（不是空谈）。
2. `State` / `Node` / `Edge` 三件套的职责边界，以及一张能贴进设计文档的对照表。
3. 一个 3 节点最小图，跑通并把结构导成 Mermaid。
4. 说清 `compile()` 之后得到的**到底是什么对象**（不是代码生成器）。

**前置检查清单**

- 2.7 的 `uv run python -m scripts.m2_agent` 能跑通（M2 已验收）。
- 会用 `TypedDict` 描述字典形状。

**难度**：★★☆☆☆ ｜ **预计时长**：75 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

先承认一件事：**手写 if-else 能跑**。2.7 的工具循环就是一段 `for` + `if`，它工作得很好。

问题是它会随着需求长大。本节用三个真实崩法说明"长大之后"会发生什么——
这三个都是我在写 demo 时**实际跑出来**的，不是假设。

### 崩法 1：状态散落 —— 早期返回悄悄丢字段

```python
def handle_v0(title, description, *, approve=False, fail=False):
    intent = _classify(title, description)
    docs = _retrieve(intent)

    if not docs:
        # 早期返回：忘了带上 intent
        return {"status": "no_docs", "draft": "资料中没有相关信息。"}

    result = _draft(docs, intent, fail=fail)
    ...
```

实测：

```
返回: {'status': 'no_docs', 'draft': '资料中没有相关信息。'}
keys: ['draft', 'status']
→ 没有 intent、没有 needs_approval。
```

**它完全合法**——没报错、类型也对（`dict`），所以 bug 会一路流到下游，
等到某个报表统计"按意图分组"时才发现有一批记录没有意图。

### 崩法 2：无法断点 —— 崩一次，全部重来

给三个步骤加计数，在第三步注入异常：

```
第一次运行：在第 3 步崩溃（下游服务超时）
  已执行步骤数: 3
重跑：ok，又执行了 3 步
  → 为了完成 3 个步骤的工作，总共执行了 6 次
```

**因为中间结果没有任何地方存下来。** 生产里这意味着：一次下游抖动，用户等待时间翻倍，
而你调用的三个外部接口全部被重复计费。

### 崩法 3：审批无处安放

```
未审批时: {'status': 'pending_approval', 'draft': '…重置密码…'}
keys: ['draft', 'status']
```

用户点"批准"之后要继续，你得自己实现三件事：

1. 把中间状态（`intent`、`docs`、`draft`）存进数据库
2. 建一个 `resume` 接口，能找到那次中断
3. 重启后（进程没了）还能接着跑

**这三件事，手写编排里一点都没准备。**

---

## 2. 核心概念与架构设计

### 2.1 类比：车间里的工序卡

- **State = 随工件流转的工序卡**：每经过一个工位，工人在卡上登记"我做了什么"
- **Node = 一个工位**：只关心"拿到卡、做完自己那步、写上结果"
- **Edge = 传送带**：规定这张卡下一步去哪

> **这个类比在哪失效**：传送带不会判断"这张卡该不该走这条线"——那是**条件边**的事（3.3）。
> 本节的图是**直线**：不管什么工件，都走完三个工位。

### 2.2 三件套的职责

| | 是什么 | 不该做什么 |
|---|---|---|
| **State** | 一份共享的、可逐步补全的状态（`TypedDict`） | 不该放"可从别处算出来"的中间值 |
| **Node** | 纯函数：读 state → 返回**要更新的字段** | 不该原地修改 state；不该返回整个 state |
| **Edge** | 显式的执行顺序 | 不该用"返回值里的 if"表达分支（那是条件边） |

**关键约束：Node 返回的是"增量"，不是"全量"。** LangGraph 会把它合并进 State。

### 2.3 图长什么样

```
    __start__
        │
        ▼
   ┌──────────┐
   │ classify │   ← 判断意图（vpn / email / other）
   └──────────┘
        │
        ▼
   ┌──────────┐
   │ retrieve │   ← 按意图取资料
   └──────────┘
        │
        ▼
   ┌──────────┐
   │  draft   │   ← 拼答复 + 判断要不要审批
   └──────────┘
        │
        ▼
     __end__
```

**注意 `__start__` / `__end__` 是 LangGraph 自动加的虚拟节点**，不是你写的。

### 2.4 `compile()` 到底得到了什么

```python
graph = builder.compile()
print(type(graph).__name__)      # CompiledStateGraph
```

**它不是代码生成器，也不是"把图变成一堆 if-else"。** 它是一个**可执行对象**，持有：

- 节点表（名字 → 函数）
- 边表（source → target）
- 入口/出口
- **后面几节要加的东西**：checkpointer（3.4）、中断点（3.5）、编译期校验

它支持 `ainvoke()` / `astream()` / `aget_state()`（后者需要 checkpointer）。

**所以"编译"这个词是借用编译器的说法，实际做的是"把声明式结构固化成可执行体 + 校验"。**

### 2.5 适用边界

| 场景 | 需要图吗 |
|---|---|
| 线性 3 步以内、无分支、无中断 | ❌ 直接写函数更清楚 |
| 有分支/循环/需要人工介入 | ✅ 强烈建议 |
| 需要断点续跑、时间旅行 | ✅ 必须 |
| 多人协作、流程要能画出来评审 | ✅ 图是沟通工具 |

> **一句话**：图的成本是"多一层抽象"，收益是"控制流变成了可以被工具处理的数据"。

---

## 3. 环境与版本

```powershell
uv add langgraph
```

实测：

```
 + langgraph==1.2.22
 + langgraph-checkpoint==4.3.2
 + langgraph-prebuilt==1.1.2
 + langgraph-sdk==0.4.14
 + ormsgpack==1.13.1
```

| 组件 | 实测版本 |
|---|---|
| langgraph | `1.2.22` |
| langgraph-checkpoint | `4.3.2` |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from langgraph.graph import StateGraph, START, END; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/minimal.py` | **主角**：State + 3 个 Node + 组装 |
| `scripts/langgraph_intro_demo.py` | 三种崩法 + 图版本 + 对比 |
| `tests/test_graph_minimal.py` | 12 个断言 |

### 4.1 State

```python
class TicketState(TypedDict, total=False):
    """图里所有节点共享的状态。

    用 `total=False` 是因为它天然是"逐步补全"的：
    刚开始只有 title，跑到后面才有 intent/docs/draft。
    """
    title: str
    description: str
    intent: str
    docs: list[str]
    draft: str
    needs_approval: bool
    trace: list[str]
```

### 4.2 Nodes

```python
def classify(state: TicketState) -> dict[str, Any]:
    """节点 1：判断意图。返回**要更新的字段**，不是整个 state。"""
    text = f"{state.get('title', '')} {state.get('description', '')}".lower()

    if any(word in text for word in _VPN_WORDS):
        intent = "vpn"
    elif any(word in text for word in _EMAIL_WORDS):
        intent = "email"
    else:
        intent = "other"

    return {"intent": intent, "trace": [*state.get("trace", []), "classify"]}
```

### 4.3 组装与编译

```python
def build_minimal_graph():
    builder = StateGraph(TicketState)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("draft", draft)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", END)

    return builder.compile()
```

### 4.4 把图导出来

```python
def describe_graph(graph: Any) -> dict[str, Any]:
    drawable = graph.get_graph()
    return {
        "nodes": [name for name in drawable.nodes if not name.startswith("__")],
        "edges": [(e.source, e.target) for e in drawable.edges
                  if not e.source.startswith("__") and not e.target.startswith("__")],
        "entry_edges": [(e.source, e.target) for e in drawable.edges if e.source.startswith("__")],
        "mermaid": drawable.draw_mermaid(),
    }
```

### 4.5 运行

```powershell
uv run python -m scripts.langgraph_intro_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 Node 返回 `dict` 而不是 `TicketState`

因为**它只该返回自己改动的部分**。返回完整 State 有两个坏处：

1. 你得把没改的字段也原样抄一遍——抄漏一个就把它清空了
2. 将来多个节点并行写不同字段时，全量返回会互相覆盖（3.2 讲 reducer 时会更明显）

`dict[str, Any]` 这个类型注解就是在说"这是增量"。

### 5.2 `total=False` 的必要性

`TypedDict` 默认所有键必填。但图的 State 是逐步长出来的：`classify` 跑之前没有 `intent`。
如果声明为必填，类型检查器会在每个节点里报"缺少字段"。

**用 `total=False` + 每个节点自己 `state.get(...)` 兜底**，是这个阶段的正确姿势。

### 5.3 `trace` 为什么是手工追加

```python
"trace": [*state.get("trace", []), "classify"]
```

因为本节的 State **没有声明任何 reducer**——后写的值会**覆盖**先写的值。
所以每个节点都得自己把历史拼上去。

这正是 3.2 要解决的问题：给 `trace` 声明"追加式 reducer"，节点就只需要返回 `{"trace": ["classify"]}`。
**先手写一遍，你才知道 reducer 在替你做什么。**

### 5.4 `CompiledStateGraph` 到底是什么

实测：

```
编译产物类型: CompiledStateGraph
节点: ['classify', 'retrieve', 'draft']
边  : [('classify', 'retrieve'), ('retrieve', 'draft')]
入口: [('__start__', 'classify')]
```

它持有节点表、边表、入口，并暴露 `ainvoke` / `astream`。**它不是把图翻译成 if-else 的代码生成器**，
而是"按边表调度节点"的执行器。

**这个区别很重要**：因为它是执行器，所以后面才能在"每执行一个节点后存一次档"（3.4 的 checkpointer）。

### 5.5 `get_graph()` 里的 `__start__` / `__end__`

LangGraph 自动加的两个虚拟节点，代表"图的入口/出口"。它们不在你的 `add_node` 里，
但会出现在 `get_graph().nodes` 中。

**写可视化或做流程评审时要过滤掉它们**，否则图会多出两个看不懂的框（`describe_graph` 已经过滤）。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：早期返回丢字段

见 §1 崩法 1。实测返回 `keys: ['draft', 'status']`，缺 `intent` 与 `needs_approval`。

**换成图之后这个 bug 在结构上就不可能发生**：`classify` 写进 State 的 `intent` 会一直在，
后续任何节点看到的是**同一份被合并过的 State**，不存在"某条路径没带上字段"。

### 6.2 实验 B：崩了重跑

见 §1 崩法 2。实测 3 步崩 + 重跑 3 步 = **6 次执行**，只为完成 3 步工作。

**换成图之后**：3.4 会给图挂 Checkpointer，每个节点执行完自动存档，崩了从最后一个成功的节点继续。
本节还没挂，所以现在跑图崩了也一样要重跑——**这一点必须诚实**：图本身不解决断点，**checkpointer 才解决**。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.langgraph_intro_demo
```

实测（节选）：

```
崩法 1：状态散落
  返回: {'status': 'no_docs', 'draft': '资料中没有相关信息。'}
  keys: ['draft', 'status']

崩法 2：无法断点
  第一次运行：在第 3 步崩溃（下游服务超时）
    已执行步骤数: 3
  重跑：ok，又执行了 3 步
    → 为了完成 3 个步骤的工作，总共执行了 6 次

崩法 3：审批无处安放
  未审批时: {'status': 'pending_approval', 'draft': '…重置密码…'}
  keys: ['draft', 'status']

换成状态图：State + Node + Edge
  编译产物类型: CompiledStateGraph
  节点: ['classify', 'retrieve', 'draft']
  边  : [('classify', 'retrieve'), ('retrieve', 'draft')]
  入口: [('__start__', 'classify')]

  输入: VPN 连不上 / 客户端提示认证失败
    意图: vpn   审批: True
    走法: classify → retrieve → draft
  输入: 邮件附件太大 / 附件 50MB 发不出去
    意图: email   审批: False
    走法: classify → retrieve → draft
  输入: 打印机坏了 / 三楼打印机不出纸
    意图: other   审批: False
    走法: classify → retrieve → draft
```

**注意第三个用例也走了三个节点**——本节是直线图，没有分支。**分支在 3.3。**

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 71%]
.........................................................                [100%]
201 passed in 23.78s
```

---

## 8. 生产环境踩坑实录

### 坑 1：在 Node 里原地修改 state

**现象**

节点写完后，下一个节点看到的还是旧值；或者更诡异——**两个请求之间互相污染**。

**定位方法**

```python
def node(state):
    state["docs"].append("x")     # ← 原地改
    return {}
```

如果 State 里有可变对象（list/dict），原地改会**改到同一份引用**。

**根因**

LangGraph 按"返回的增量"合并 State。原地修改既不会被识别为更新，
又可能改到共享引用（尤其当初始值是模块级默认值时）。

**修法**

```diff
- state["docs"].append("x")
- return {}
+ return {"docs": [*state.get("docs", []), "x"]}
```

**顺手加固**：把 Node 全写成纯函数（读 state → 返回新值），并在测试里断言"调用前后 state 不变"
（本节 `test_classify_does_not_mutate_input` 就是干这个的）。

### 坑 2：Node 返回整个 State

**现象**

某个节点的产出总是覆盖掉前面节点写的东西。

**根因**

```python
def node(state):
    state["intent"] = "vpn"
    return state          # ← 返回全量
```

返回全量时，你把"当前读到的旧值"也一并写回去了。如果这期间别的分支更新了某些字段，
就会被这份快照覆盖。

**修法**

只返回你改的字段：

```diff
- return state
+ return {"intent": "vpn"}
```

### 坑 3：以为 `compile()` 会生成代码

**现象**

有人问"编译后的图在哪个文件里？我能改吗？"——没有那个文件。

**根因**

把 `compile()` 类比成 `gcc`。实际它做的是**校验 + 固化成执行器**。

**记住了**：`CompiledStateGraph` 是运行时对象，能 `ainvoke`、能 `astream`、能 `get_graph()`。
**没有生成的 Python 代码可读。**

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：Node 为什么只返回"增量"，不返回整个 State？</b></summary>

因为返回全量会把"读到的旧值"一并写回，覆盖掉期间别处更新的字段；而且全量要求你把没改的字段也抄一遍，抄漏就清空。返回增量的语义是"我只声明我改了什么"，合并交给 LangGraph。

</details>

<details>
<summary><b>Q2：`compile()` 之后得到的是什么？</b></summary>

一个 `CompiledStateGraph` 执行器：持有节点表、边表、入口/出口，暴露 `ainvoke`/`astream`。它**不是代码生成器**，没有生成可读的 Python 代码。正因为它是"执行器"而不是"生成的代码"，才能在节点之间插入"存一次档"（3.4 的 checkpointer）。

</details>

<details>
<summary><b>Q3：既然图不解决断点和审批，为什么还要换？</b></summary>

因为它是**前提**。断点需要在"每个节点执行完"这个时机存档——手写 if-else 没有这个时机；审批需要"挂起后还能恢复"——手写编排没有可恢复的状态容器。图把控制流变成了数据结构，后面三节（3.3 分支、3.4 断点、3.5 审批）才能挂上去。

</details>

### 9.2 动手任务：画出你自己业务的那张图

**要求**

1. 挑你手上一条真实的业务流程（≥ 4 步）。
2. 定义它的 `State`：列出所有字段，并标注每个字段**由哪个节点写入**。
3. 画出节点与边，用 `describe_graph()` 导出的 Mermaid 作为交付物。
4. 用文字标出**所有可能的分支**（现在还不实现，只要标出来）。

**验收标准**

- `State` 里每个字段都能指出"谁写它、谁读它"；没有字段是"谁都不写"。
- Mermaid 图能渲染，节点数与你的业务流程步骤数一致。
- 分支清单里每一分支都写清"判断条件"和"去哪"。
- 能说出至少一个"不该放进 State"的字段，并解释为什么。

**提示**：如果某个字段是"另一个字段算出来的"（比如 `needs_approval` 由 `intent` + `draft` 决定），
考虑不放进 State——多存一份就多一个不一致的机会。

### 9.3 拓展挑战

给 `build_minimal_graph()` 加一个"重试"节点：当 `draft` 为空时，回到 `retrieve` 再试一次，最多两次。

**思考**：

1. 不加最大次数限制会怎样？（试试让它永远失败）
2. 这种"回到前面节点"的边，和 3.3 的条件边是同一个机制吗？
3. 如果图能无限循环，LangGraph 有内置的保护吗？（提示：`recursion_limit`）

---

## 10. 本节小结与延伸

**三点回顾**

1. **手写编排的三个崩法都是"状态没有容器"导致的**：早期返回丢字段（实测 `keys` 少两个）、崩了重跑（实测 6 次执行做 3 步活）、审批只能返回 pending 然后丢光上下文。
2. **State 集中、Node 纯函数、Edge 显式**。Node 只返回增量；`trace` 现在还要手工拼，3.2 会换成 reducer。
3. **`compile()` 得到的是执行器**（`CompiledStateGraph`），不是代码生成器——正因为它能"逐节点调度"，后面才能挂 checkpointer 与中断。

**已知限制（诚实记录）**

- **本节是直线图**：三个用例都走完三个节点，没有分支（3.3）。
- **没有 checkpointer**：现在崩了照样要重跑，图本身不解决断点（3.4）。
- **`trace` 是手工追加**：等 3.2 引入 reducer 后才自然。
- **版本与大纲不一致**：实测 `langgraph 1.2.22`，大纲曾按 `0.6.x` 规划，API 形状一致但版本号已漂移。

**延伸阅读**

- LangGraph 官方文档（Graph API）：https://langchain-ai.github.io/langgraph/
- `StateGraph` 与 `CompiledStateGraph`：https://langchain-ai.github.io/langgraph/reference/graphs/
- Mermaid 流程图语法（用于设计评审）：https://mermaid.js.org/syntax/flowchart.html

---

## 修订记录

- 2026-11-18 初稿：三种崩法与最小图全部实机运行，§7 为真实输出
- 2026-11-18 修正：崩法 3 的示例资料里原本没有"重置"二字，导致 `needs_approval` 恒为 False、演示不成立；补上"自助门户重置密码"一句
- 2026-11-18 修正：对比表中文宽度导致列错位，改为按显示宽度补齐
- 2026-11-18 记录：实测 `langgraph 1.2.22`（大纲曾假设 0.6.x），API 形状一致
