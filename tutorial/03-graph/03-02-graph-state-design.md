---
section: "3.2"
title: "设计 State：什么才是「真状态」"
stage: 3
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M3
depends_on: ["3.1"]
produces: ["graph/state.py"]
verifies: "能解释 messages 为何必须用追加 reducer，改错后复现消息丢失"
part: null
updated: "2026-11-18"
---

# 3.2 设计 State：什么才是「真状态」

## 0. 本节导航

**本节在链路中的位置**：3.1 的图里，`trace` 还是靠手工拼接的（`[*state["trace"], "classify"]`）。本节解释**为什么那是不对的**，并给出正确的做法。3.4 的 checkpointer 存什么、3.5 的中断恢复靠什么，全都取决于这里的设计。

**本节你将获得**

1. 一条默认规则：**没有 reducer 的字段，后写的会整个覆盖先写的**。
2. 三种 reducer：`operator.add`、`add_messages`、自己写的合并函数。
3. `messages` 必须用 `add_messages` 的**两个**理由（历史 + 同 id 替换）。
4. 一个判断表：哪些字段该进 State，哪些该现算。
5. 三个实测事故：trace 只剩最后一步、messages 只剩最后一条、`needs_approval` 自相矛盾。

**前置检查清单**

- 3.1 的 `uv run python -m scripts.langgraph_intro_demo` 能跑通。
- 知道 `TypedDict` 与 `Annotated` 的基本用法。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

用 LangGraph 的第一周，几乎所有人都会遇到同一个问题：

> **"我上一个节点写进 State 的东西，下一个节点读不到了。"**

你会怀疑：是不是节点没执行？是不是返回值写错了？是不是 State 声明有问题？

**真实原因通常是：你给这个字段写了两次，而第二次把第一次覆盖了。**

实测（两个节点，都写 `trace`）：

```
无 reducer : trace = ['node-b']      ← node-a 的痕迹没了
有 reducer : trace = ['node-a', 'node-b']
```

**这不是 bug，是设计。** 想象一下 `intent` 字段：`classify` 写入 `"vpn"`，后来人工改判为 `"email"`，
你**希望**后写的生效——这就是"默认覆盖"的正确用途。

所以问题不是"默认覆盖错了"，而是**你没区分"该覆盖的字段"和"该累积的字段"**。

更危险的版本在 `messages` 上。实测：

```
无 reducer : 1 条 → ['assistant']
有 reducer : 2 条 → ['human', 'ai']
```

**用户的原始问题被覆盖掉了。** 模型看到的历史里只有自己说的"好的"，
它根本不知道自己刚才在回应什么——然后开始胡说。

---

## 2. 核心概念与架构设计

### 2.1 类比：白板 vs 日志本

- **默认覆盖 = 白板**：谁最后写，就以谁为准。适合"当前结论"这类信息（`intent`、`draft`）。
- **reducer = 日志本**：每次写入都是**追加一条**，历史全留着。适合"过程"这类信息（`trace`、`messages`）。

> **这个类比在哪失效**：日志本也不是纯粹追加——`add_messages` 遇到**同 id** 的消息会**替换**那一行
> （相当于"修改日志里的某一笔记录"）。这是它比 `operator.add` 多出来的能力，下面会讲。

### 2.2 一张表看懂三种写入语义

| 字段类型 | 默认行为 | 需要 reducer 吗 | 例子 |
|---|---|---|---|
| 标量（str/bool/int） | 覆盖 | ❌ 不需要 | `intent`、`draft` |
| 列表（累积） | 覆盖（**整个列表被替换**） | ✅ 需要 | `trace` |
| 字典（合并） | 覆盖（**整个字典被替换**） | ✅ 需要（自定义） | `sources` |
| 消息列表 | 覆盖（**历史全丢**） | ✅ 必须 `add_messages` | `messages` |

**一句话**：**凡是被两个以上节点写同一个字段，且你想保留每次写入，就必须声明 reducer。**

### 2.3 `messages` 为什么必须是 `add_messages`

**理由一：保留历史**（上面实测过）。

**理由二：支持"改同一条"而不是"再加一条"。** 实测：

```
同 id 更新 : 1 条 → ['草稿 v2']
```

`add_messages` 按消息的 `id` 做 upsert：

- 没有同 id → 追加
- 有同 id → **替换那一条**（位置不变）

这在"润色回答""修正工具调用"这类场景里是刚需。用 `operator.add` 会让聊天记录里
出现两条内容互相矛盾的助手消息。

### 2.4 自定义 reducer 的签名

```python
def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}
```

两个参数：**已有的值**、**本次写入的值**。返回合并结果。

**注意 `None`**：字段首次写入时，LangGraph 传进来的 `left` 是 `None`。不处理会直接 `AttributeError`。

### 2.5 什么才算"真状态"

三个问题，任何一个答"是"就不该放进 State：

| 问题 | 例 | 处理 |
|---|---|---|
| 能由别的字段算出来吗？ | `needs_approval` = f(`intent`, `draft`) | **现算**，不存 |
| 只是某次调用的临时变量吗？ | 循环下标、临时 buffer | 放节点内部 |
| 是不是"谁都不写"的字段？ | 定义了但从没赋值的 | 删掉 |

**实测后果**（§6.3）：把 `needs_approval` 存进 State 后，改了 `intent` 忘了同步它：

```
改之前: {'intent': 'vpn', 'draft': '先重置密码', 'needs_approval': True}
改了 intent 之后: {'intent': 'email', 'draft': '先重置密码', 'needs_approval': True}
自相矛盾吗: needs_approval=True 但 derive()=False
```

**两份真相，必然有一天打架。**

### 2.6 适用范围（边界）

| 场景 | 建议 |
|---|---|
| 单节点写、单节点读 | ✅ 默认覆盖够用 |
| 多节点写同一字段，要保留每次 | ✅ reducer |
| 想在 checkpointer 里回看"每一步怎么变的" | ✅ `trace` + reducer（3.4 会用） |
| 想省存储 | ❌ 别把可推导的 `trace` 当必需品（生产里它也可以只进日志） |

---

## 3. 环境与版本

本节**不新增依赖**（`langgraph` 已在 3.1 装好）。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| langgraph | `1.2.22` | `StateGraph` / `add_messages` |
| Python | `3.12.13` | `Annotated` / `operator.add` |

**验证**

```powershell
uv run python -c "from langgraph.graph.message import add_messages; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/state.py` | **主角**：State 定义 + 自定义 reducer + 派生函数 |
| `scripts/state_design_demo.py` | 五步演示 |
| `tests/test_graph_state.py` | 16 个断言 |

### 4.1 State 定义

```python
class TicketState(TypedDict, total=False):
    # ---- 输入 ----
    ticket_id: str
    title: str
    description: str

    # ---- 累加型：必须给 reducer ----
    # 对话历史：为什么必须用 add_messages 而不是 operator.add？
    # 因为 add_messages 还认 id —— 同 id 的消息会「替换」而不是「重复追加」。
    messages: Annotated[list[dict[str, Any]], add_messages]

    # 执行轨迹：纯追加，operator.add 就够了
    trace: Annotated[list[str], operator.add]

    # 证据来源：chunk_id → 文档名，用自定义 reducer 合并
    sources: Annotated[dict[str, Any], merge_dicts]

    # ---- 覆盖型：默认行为就是"后来者胜"，不需要 reducer ----
    intent: str
    draft: str
```

### 4.2 自定义 reducer

```python
def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """合并两个字典，右边的同名键覆盖左边。

    注意两个参数都可能是 None —— LangGraph 在字段首次写入时传 None。
    """
    return {**(left or {}), **(right or {})}
```

### 4.3 派生字段写成函数

```python
def derive_needs_approval(intent: str | None, draft: str | None) -> bool:
    """由 State 里已有的字段推导"要不要审批"。

    这是「可重算的中间量」的标准处理方式：**不存，现算**。
    """
    return intent == "vpn" and "重置" in (draft or "")
```

### 4.4 把写入语义列成文档

```python
def describe_state_schema() -> dict[str, str]:
    annotations = TicketState.__annotations__
    result: dict[str, str] = {}
    for name in annotations:
        if name in ("messages", "trace", "sources"):
            result[name] = "累加（有 reducer）"
        else:
            result[name] = "覆盖（后来者胜）"
    result["needs_approval"] = "不存（derive_needs_approval 现算）"
    return result
```

### 4.5 运行

```powershell
uv run python -m scripts.state_design_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 reducer 的调用时机

**每次合并都调用一次**，不是"最后统一合并"。

单节点内也是一样：如果节点返回 `{"trace": ["x"]}`，LangGraph 会立刻调用
`reducer(旧值, ["x"])`。所以：

- reducer 必须是**纯函数**（不改入参）
- reducer 必须能处理 `None`（首次写入）
- reducer 必须**幂等安全**：同一次合并被重放时结果一致（3.4 的恢复会重放）

### 5.2 为什么 reducer 不能修改入参

```python
# ❌ 会让历史重复
def bad_add(left, right):
    left.extend(right)      # 改了入参
    return left
```

因为 `left` 可能就是 State 里那个列表对象。你一 `extend`，State 已经被改了，
然后 LangGraph 又把返回值存回去——**同一条记录出现两次**。

**正确写法**：永远返回新对象（`[*left, *right]`）。

### 5.3 `add_messages` 会把 dict 变成 `BaseMessage`

这是我在写 demo 时踩的：

```
TypeError: 'HumanMessage' object is not subscriptable
```

因为 `add_messages` 会把 `{"role": "user", ...}` 转成 `HumanMessage` 对象。
转换之后：

- 读内容要用 `.content`，不是 `["content"]`
- 读角色要用 `.type`（值是 `human` / `ai`），不是 `.role`（`role` 是 `user` / `assistant`）

**这段坑很值得记**：类型悄悄变了，但报错信息完全不提"我把它转了"。

### 5.4 `total=False` 与 reducer 的关系

`total=False` 只是让**键可选**，和 reducer 无关。但如果声明为 `total=True`（默认），
你会被迫在每个节点返回时补齐所有键——那就变成 3.1 说的"返回全量"反模式了。

### 5.5 `describe_state_schema()` 为什么要存在

因为 State 的设计**必须能被评审**。

代码里 `Annotated[list[str], operator.add]` 这种写法，评审时很容易被眼睛跳过。
把它列成"字段 → 写入语义"的表，`intent` 该覆盖、`trace` 该累加，
**一眼就能看出哪里漏了 reducer**。

（真实项目里这张表应该进 ADR 或设计文档，而不是留在代码里。）

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：`trace` 不加 reducer

```python
class PlainState(TypedDict):
    trace: list[str]
```

两个节点分别返回 `{"trace": ["node-a"]}` 和 `{"trace": ["node-b"]}`。实测：

```
无 reducer : trace = ['node-b']
有 reducer : trace = ['node-a', 'node-b']
```

**代价**：你失去了"这个请求到底走了哪条路径"的能力。而排查线上问题时，
**执行路径往往比最终结果更重要**。

### 6.2 实验 B：`messages` 不加 reducer（最致命）

实测：

```
无 reducer : 1 条 → ['assistant']
有 reducer : 2 条 → ['human', 'ai']
```

用户的原始问题被覆盖。模型在下一轮看到的历史里**只有自己说过的话**。

后果不是"报错"，而是**模型开始答非所问**——而你会去查 prompt、查模型版本、查温度参数，
唯独不会怀疑"历史只有一条"。

### 6.3 实验 C：把派生字段存进 State

实测：

```
改之前: {'intent': 'vpn', 'draft': '先重置密码', 'needs_approval': True}
改了 intent 之后: {'intent': 'email', 'draft': '先重置密码', 'needs_approval': True}
自相矛盾吗: needs_approval=True 但 derive()=False
```

**同样的输入，两个不同的答案。** 哪一个对？取决于你问谁——这就是"两份真相"的定义。

而如果只存原料：

```
intent=vpn    draft=先重置密码    → needs_approval=True
intent=email  draft=先重置密码    → needs_approval=False
intent=vpn    draft=只是查询      → needs_approval=False
```

**永远自洽。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.state_design_demo
```

实测（节选）：

```
第 1 步：默认覆盖 —— 两次写入只剩最后一次
  无 reducer : trace = ['node-b']      ← node-a 的痕迹没了
  有 reducer : trace = ['node-a', 'node-b']

第 2 步：messages —— 为什么必须是 add_messages
  无 reducer : 1 条 → ['assistant']
  有 reducer : 2 条 → ['human', 'ai']
  同 id 更新 : 1 条 → ['草稿 v2']

第 3 步：自定义 reducer —— 字典合并
  合并结果: {'c1': 'vpn-guide-v2', 'c2': 'email-faq'}

第 4 步：可重算的字段，别存
    改之前: {'intent': 'vpn', 'draft': '先重置密码', 'needs_approval': True}
    改了 intent 之后: {'intent': 'email', 'draft': '先重置密码', 'needs_approval': True}
    自相矛盾吗: needs_approval=True 但 derive()=False

第 5 步：把 State 的写入语义列出来
  messages         累加（有 reducer）
  trace            累加（有 reducer）
  sources          累加（有 reducer）
  intent           覆盖（后来者胜）
  needs_approval   不存（derive_needs_approval 现算）
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 99%]
.                                                                        [100%]
217 passed in 23.75s
```

---

## 8. 生产环境踩坑实录

### 坑 1：`messages` 少了 reducer，模型开始答非所问

**现象**

多轮对话里，模型反复问"你刚才说什么？"或者回答上一轮的问题。

**定位方法**

在节点里打一行：

```python
log.info("state_messages", count=len(state["messages"]),
         roles=[getattr(m, "type", None) for m in state["messages"]])
```

如果 `count` 永远是 1，就是这个坑。

**根因**

`messages: list[dict]` 没有 `Annotated[..., add_messages]`，每个节点的返回把历史整个替换了。

**修法**

```diff
- messages: list[dict[str, Any]]
+ messages: Annotated[list[dict[str, Any]], add_messages]
```

**顺手加固**：在测试里断言"跑完两个节点后 `len(messages) == 2`"，把这类回归挡在 CI。

### 坑 2：自定义 reducer 改了入参，记录重复

**现象**

`trace` 里同一步出现两次；跑得次数越多重复越多。

**根因**

```python
def bad(left, right):
    left.extend(right)     # ← 改了 State 里的那个列表
    return left
```

**修法**

```diff
- left.extend(right); return left
+ return [*left, *right]
```

**顺手加固**：reducer 全部写成"入参只读、返回新对象"，并在测试里断言"调用后入参未变"
（`test_merge_dicts_does_not_mutate_inputs` 就是干这个的）。

### 坑 3：把 `add_messages` 当普通列表用

**现象**

```
TypeError: 'HumanMessage' object is not subscriptable
```

**根因**

`add_messages` 把 dict 转成了 `BaseMessage`。后续代码仍按 dict 访问 `["content"]`。

**修法**

用 `getattr(message, "content", None)`，或用 `langchain_core.messages` 的辅助函数统一处理。

**顺手加固**：State 里 `messages` 的类型注解写清楚是"消息对象列表"，
并在项目里定义一个 `role_of()` / `content_of()` 工具函数，避免各处手写兼容逻辑。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 <code>messages</code> 必须用 <code>add_messages</code>，而 <code>trace</code> 用 <code>operator.add</code> 就够？</b></summary>

`trace` 只需要"追加字符串"，`operator.add` 正好。`messages` 还需要**按 id 替换**：润色回答、修正工具调用时，你要改的是同一条消息而不是再加一条。`add_messages` 提供这个 upsert 语义，`operator.add` 只会重复追加导致历史里出现两条矛盾的助手消息。

</details>

<details>
<summary><b>Q2：默认覆盖为什么不是"设计缺陷"？</b></summary>

因为对 `intent`、`draft` 这类"当前结论"字段，后写的**就应该**生效。缺陷不在默认值，而在"你没区分哪些字段需要累加"。判据是：这个字段被两个以上节点写、且你需要保留每次写入吗？需要就声明 reducer。

</details>

<details>
<summary><b>Q3：怎样判断一个字段该不该进 State？</b></summary>

三问：① 能由别的字段算出来吗（`needs_approval`）？② 只是某次调用的临时变量吗？③ 是不是谁都不写的字段？任何一问为"是"，就不该进。存两份可推导的真相，迟早会不一致。

</details>

### 9.2 动手任务：给 3.1 的图接上 reducer

**要求**

1. 把 `minimal.py` 的 `trace` 改成 `Annotated[list[str], operator.add]`，节点只返回 `{"trace": ["classify"]}`。
2. 给 `TicketState` 加 `sources` 字段（`Annotated[dict, merge_dicts]`），在 `retrieve` 里写入 `{chunk_id: doc_id}`。
3. 把 `needs_approval` 从 State 里删掉，改成 `derive_needs_approval(intent, draft)`，在消费处现算。
4. 补 3 个用例：`trace` 累积正确、`sources` 多节点合并、`derive` 与旧字段语义一致。

**验收标准**

- 三个路径（vpn/email/other）跑完后 `trace == ["classify","retrieve","draft"]`。
- `sources` 在两次检索后是合并结果，不是最后一次。
- `TicketState` 里**没有** `needs_approval` 字段，但下游能拿到同样的布尔值。
- 原有 217 个用例仍全绿。

**提示**：第 3 步会牵动 3.1 的 `draft` 节点与测试，**注意同步修改**——这正是"删字段比加字段难"的体现。

### 9.3 拓展挑战

给 `messages` 写一个**带 token 预算的 reducer**：当累计长度超过阈值时，把最早的消息压缩成一条摘要。

**思考**：

1. reducer 里能调用 LLM 吗？（提示：reducer 必须是纯函数、同步、可重放）
2. 如果不能，摘要该在哪做？（提示：做成一个节点，返回一条替换消息）
3. 压缩后 `id` 怎么处理才能让 `add_messages` 正确地"删旧加新"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **默认是覆盖，且这是对的**——错的是没区分"该覆盖"和"该累积"。实测 `trace` 不加 reducer 只剩 `['node-b']`。
2. **`messages` 必须用 `add_messages`**：它既保留历史（否则只剩 1 条），又支持按 `id` 替换（润色场景）。
3. **能算出来的不要存**：把 `needs_approval` 存进 State 后，改 `intent` 忘了同步就出现"`True` 与 `False` 并存"。

**已知限制（诚实记录）**

- **本节未接 checkpointer**：`trace` 攒在内存里，进程一停就没了（3.4）。
- **`sources` 的合并策略是"右边赢"**：如果两个节点写同一个 chunk_id 的不同信息，会丢失左边（需要更复杂的合并语义）。
- **`messages` 的类型会变**（dict → `BaseMessage`），跨节点手工处理时容易踩坑（§5.3）。
- **未做 token 预算压缩**（§9.3）。

**延伸阅读**

- LangGraph 的 State 与 reducer：https://langchain-ai.github.io/langgraph/concepts/low_level/#state
- `add_messages` 的 upsert 语义：https://langchain-ai.github.io/langgraph/reference/graphs/#langgraph.graph.message.add_messages
- `TypedDict` 与 `Annotated`：https://docs.python.org/3/library/typing.html#typing.Annotated

---

## 修订记录

- 2026-11-18 初稿：五步演示与 16 个测试全部实机运行，§7 为真实输出
- 2026-11-18 记录：`add_messages` 会把 dict 转成 `BaseMessage`，demo 里按 dict 取值触发
  `TypeError: 'HumanMessage' object is not subscriptable`，已改为 `role_of()` / `content_of()` 兼容读取
