---
section: "3.3"
title: "条件边与循环：让图会「回头看」"
stage: 3
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M3
depends_on: ["3.2"]
produces: ["graph/edges.py"]
verifies: "构造「信息不足 → 回检索」循环且不会无限转"
part: null
updated: "2026-11-18"
---

# 3.3 条件边与循环：让图会「回头看」

## 0. 本节导航

**本节在链路中的位置**：3.1 的图是直线，3.2 让状态能被正确累积。本节让图**会分叉、会回头**——这是真实流程的常态：意图不同走不同分支、资料没查到要再查一次、下游挂了要走兜底。3.5 的人工审批也会用到这里的条件边。

**本节你将获得**

1. `add_conditional_edges` 的三要素：router 函数、分支映射、返回值的约束。
2. 循环的正确写法（自环边）与**三种刹车**：业务上限、递归上限、时间预算。
3. 一个反直觉事实：**LangGraph 默认递归上限是 10007 步**（不是 25），靠它兜底等于没有刹车。
4. 错误边的惯例做法：节点内 try/except → 写 `state.error` → 条件边导到兜底节点。
5. `Command(goto=...)` 与条件边的取舍。

**前置检查清单**

- 3.2 的 `uv run python -m scripts.state_design_demo` 能跑通。
- 知道 3.2 的 `Annotated[list, operator.add]` 是干什么的（本节 `trace` 用它）。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

3.1 结束时，三个用例（vpn / email / other）**都走完三个节点**，产出却各不相同：

- vpn → 给了 VPN 的答案
- email → 给了邮箱的答案
- other → 也走了 `retrieve` 和 `draft`，只是拿不到资料，最后说"没有相关信息"

问题很明显：

**① 不该做的事做了。** "打印机坏了"这个明显不归我们管的问题，也走了检索与拼装——白烧一次检索。

**② 该回头的时候没回头。** 如果第一次检索因为下游抖动返回空，直线图只能直接给"查不到"。
真实系统里这时候应该**再查一次**——而不是把一次抖动变成一次用户可见的失败。

**③ 出错只能整图崩。** 检索服务挂了，异常会冒到 `ainvoke` 的调用方，
用户看到 500。而正确做法是：**图内接住，走一个能说人话的兜底节点**。

这三个问题的共同点：**控制流被写死在直线里了。** 本节就是把控制流变成"可判断、可回退、可兜底"的边。

---

## 2. 核心概念与架构设计

### 2.1 类比：地铁换乘图

- **普通边** = 单程直达：这一站完了就去下一站
- **条件边** = 换乘指示牌：看你的票（State）决定走哪条线
- **自环边** = 环线：绕回同一个站（重试）
- **兜底节点** = 终点站的服务中心：出事了来这儿，有人能帮你

> **这个类比在哪失效**：地铁不会因为"你没带证件"让你原地绕圈到放弃。
> **循环必须有刹车**，而地铁的刹车是"末班车时间"。本节的三道刹车见 §2.4。

### 2.2 条件边的三要素

```python
builder.add_conditional_edges(
    "classify",                    # ① 从哪个节点出来
    route_by_intent,               # ② router：读 state，返回一个标签
    {"vpn": "fetch_vpn",           # ③ 标签 → 目标节点
     "email": "fetch_email",
     "other": "clarify"},
)
```

**约束**：router 的返回值**必须是映射里的 key**。返回一个不存在的标签，就找不到目标。

### 2.3 循环就是"自环边"

```python
builder.add_conditional_edges(
    node,
    route_after_fetch,
    {"answer": "answer",
     "retry": node,        # ← 回到自己，这就是循环
     "clarify": "clarify",
     "fallback": "fallback"},
)
```

**没有特殊语法**：把边连回已经走过的节点，就是循环。图允许这么做。

### 2.4 三道刹车（**本节最容易被跳过的一节**）

| 刹车 | 怎么设 | 好处 | 短处 |
|---|---|---|---|
| **业务上限** | `max_attempts` 存在 State 里 | 能说出"为什么放弃" | 需要自己写判断 |
| **递归上限** | `ainvoke(..., {"recursion_limit": N})` | 框架自带，兜底 | 只能给异常，说不出业务原因 |
| **时间预算** | 在节点里检查 `deadline` | 控制延迟 | 需要自己实现 |

**实测一个关键事实**：LangGraph 的默认递归上限是 **10007 步**，不是很多人以为的 25。

```
抛出: GraphRecursionError
信息: Recursion limit of 12 reached without hitting a stop condition.
```

（上面是显式传 `recursion_limit=12` 的结果。不传的话，要跑 10007 步才会停。）

**10007 步是什么概念**：如果每步调一次 LLM（按 1 秒算），那是近 3 小时、上万次调用。
**递归上限适合当"最后一道保险"，绝对不能当业务刹车。**

### 2.5 错误边：LangGraph 没有"自动捕获异常的边"

这是新手最容易误解的地方。你要自己实现三件事：

```python
def fetch(state):
    try:
        docs = await risky_call()
    except TimeoutError as exc:
        return {"error": str(exc), "attempts": attempts}    # ① 写进 state
    return {"docs": docs}

def route_after_fetch(state):
    if state.get("error"):
        return "fallback"                                    # ② 条件边导过去
    ...
```

③ 兜底节点给出一句**用户能行动**的话（"请稍后重试或转人工"），而不是把堆栈丢给用户。

**为什么不用 try/except 包住整个 `ainvoke`**：那样你只知道"失败了"，
不知道失败时状态走到哪、已经花了多少钱、重试会不会重复副作用。

### 2.6 条件边 vs `Command`

| | 条件边 | `Command(goto=...)` |
|---|---|---|
| 路由写在哪 | 独立的 router 函数 | 节点内部 |
| 图结构可见性 | ✅ `add_conditional_edges` 一眼看全 | ❌ 藏在节点返回值里 |
| 判断与动作是否在一起 | ❌ 分离 | ✅ 在一起 |
| 适合 | 分支多、需要评审图结构 | 判断和动作强耦合、分支少 |
| 与 State 更新 | router 只读，不能改 state | `Command(goto=..., update={...})` 一次做完 |

**建议**：默认用条件边（可读、可评审）；只有当"算完就知道去哪"时用 `Command`。

### 2.7 适用范围（边界）

| 场景 | 用什么 |
|---|---|
| 意图分流（vpn / email / 其他） | 条件边 |
| 重试（资料没拿到 / 下游抖动） | 自环边 + 业务上限 |
| 下游故障 | try/except + 条件边 + 兜底节点 |
| 需要"失败后换个工具再试" | `Command` 更顺手（判断与动作在一起） |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| langgraph | `1.2.22` | `add_conditional_edges` / `Command` |
| Python | `3.12.13` | `Literal` / `operator.add` |

**验证**

```powershell
uv run python -c "from langgraph.types import Command; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/edges.py` | **主角**：分支路由 + 重试 + 兜底 + Command 版 |
| `scripts/edges_demo.py` | 七步演示 |
| `tests/test_graph_edges.py` | 18 个断言 |

### 4.1 路由函数

```python
def route_by_intent(state: RouteState) -> Literal["vpn", "email", "other"]:
    intent = state.get("intent", "other")
    return intent if intent in ("vpn", "email") else "other"


def route_after_fetch(state: RouteState) -> Literal["answer", "retry", "clarify", "fallback"]:
    """取完资料之后去哪：成功 → answer；可重试 → retry；超限 → clarify；出错 → fallback。"""
    if state.get("error"):
        return "fallback"
    if state.get("docs"):
        return "answer"
    if state.get("attempts", 0) < state.get("max_attempts", 3):
        return "retry"
    return "clarify"
```

**注意判断顺序**：`error` 优先于 `docs`。**错误不能被"反正拿到资料了"给吞掉。**

### 4.2 组装（含自环）

```python
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"vpn": "fetch_vpn", "email": "fetch_email", "other": "clarify"},
    )
    for node in ("fetch_vpn", "fetch_email"):
        builder.add_conditional_edges(
            node,
            route_after_fetch,
            {"answer": "answer",
             "retry": node,          # ← 循环：回到自己
             "clarify": "clarify",
             "fallback": "fallback"},
        )
```

### 4.3 Command 版

```python
    def smart_fetch(state: RouteState) -> Command:
        update = _fetch(state, state.get("intent", "other"), empty_rounds=empty_rounds)

        if update.get("error"):
            return Command(goto="fallback", update=update)
        if update.get("docs"):
            return Command(goto="answer", update=update)
        if update.get("attempts", 0) < state.get("max_attempts", 3):
            return Command(goto="smart_fetch", update=update)   # 自环
        return Command(goto="clarify", update=update)
```

### 4.4 运行

```powershell
uv run python -m scripts.edges_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 router 只读 State，不能改

条件边的 router 是**纯判断**，返回值只是一个标签。它**不能**更新 State。

要"边判断边写状态"（比如"我要重试，顺便把 attempts +1"），把写状态放在**节点里**，
router 只看结果——或者用 `Command(goto=..., update=...)` 一次做完。

### 5.2 自环边为什么不会无限跑

因为它不是"自动重跑"，而是**又一次进入节点**。每次进入都会重新执行节点函数、
重新写 State。**所以刹车必须在节点或 router 里判断。**

本节把刹车放在 router（`attempts < max_attempts`），因为它离"决策"最近。

### 5.3 `attempts` 为什么在节点里 +1，而不是在 router 里

因为 router 不该有副作用。如果 router 里 `state["attempts"] += 1`，
那么"重放一次 router"（3.4 的恢复会重放）就会多加一次，**计数漂移**。

**规则：写状态只在节点里；router 只读。**

### 5.4 为什么 `error` 优先于 `docs`

实测（`test_error_takes_priority_over_docs`）：

即使 `docs` 有内容，只要 `error` 存在，也先去兜底。

**理由**：部分成功比完全失败更危险。你拿着一半的资料给出答案，用户以为它是完整的。
**宁可整单失败并明说，也不要给一个"看起来完整"的半成品。**

### 5.5 `Command` 的自环怎么写

```python
return Command(goto="smart_fetch", update=update)
```

`goto` 指向**自己**，就是自环。因为 `update` 里带了 `attempts`，
下一次进入时 router 的等价判断（节点内的 `if`）就能看到新值。

**注意**：`Command` 版把"判断"也放进了节点，所以**图的边表里看不到 `retry` 这条边**——
这就是 §2.6 说的"结构可见性"代价。§7 的 `describe_routes` 里能直接看到这个差别。

### 5.6 兜底节点为什么必须存在

因为"出错"是常态。没有兜底，异常会冒到 `ainvoke` 的调用方：

- 你失去"已经走到哪一步"的信息
- 你失去"这次花了多少钱"的信息
- 用户看到的是一句技术错误

兜底节点让图**永远返回一个可展示的答案**，同时把 `error` 留在 State 里供排查。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：去掉 `max_attempts`

把刹车去掉（`empty_rounds=10**9`、`max_attempts=10**9`），只靠递归上限：

```
抛出: GraphRecursionError
信息: Recursion limit of 12 reached without hitting a stop condition.
```

（这是显式传 `recursion_limit=12` 的结果。）

**注意我在这节踩的一个坑**：我原本在 demo 里写"默认上限 25 步"——**这是错的**。
实测不传 `recursion_limit` 时，要跑到 **10007 步**才抛。已改正。

**10007 步的代价**：每步一次下游调用，就是上万次请求。**账单和下游都会被打爆。**

### 6.2 实验 B：把错误当成"空结果"

如果把 `_fetch` 里的错误分支删掉，让下游异常直接变成"返回空 docs"：

- 路由会判定 `retry`，重试 `max_attempts` 次
- 然后判定 `clarify`，用户看到 **"还需要更多信息才能判断"**

**但真实原因是"检索服务挂了"。** 用户会去补充信息（他以为是自己没说清），
补了也没用；而你这边**一条告警都不会有**——因为图跑得很"成功"。

> 这就是"错误必须显式化"的理由：**把故障伪装成业务失败，是最难排查的一类 bug。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.edges_demo
```

实测（节选）：

```
第 1 步：三路分支
  「VPN 连不上」    走法: classify → fetch_vpn → answer      尝试 1
  「邮件发不出」    走法: classify → fetch_email → answer    尝试 1
  「打印机坏了」    走法: classify → clarify                 尝试 0

第 2 步：循环
  走法    : classify → fetch_vpn → fetch_vpn → answer
  尝试次数: 2

第 3 步：刹车
  走法    : classify → fetch_vpn → fetch_vpn → fetch_vpn → clarify
  尝试次数: 3

第 4 步：错误边
  走法    : classify → fetch_vpn → fallback
  答复    : 查询暂时失败（vpn 检索服务超时），请稍后重试或转人工。

第 5 步：没有刹车
  抛出: GraphRecursionError
  信息: Recursion limit of 12 reached without hitting a stop condition.

第 6 步：Command
  走法    : classify → fetch_vpn → fetch_vpn → answer
```

结构导出（第 7 步，条件边的分支标签）：

```
  classify       → clarify       [other]
  classify       → fetch_email   [email]
  classify       → fetch_vpn     [vpn]
  fetch_vpn      → fetch_vpn     [retry]
  fetch_vpn      → answer
  fetch_vpn      → clarify
  fetch_vpn      → fallback
```

**这一张表就是"改之前先看结构"的工具**：分支漏了、循环没刹车、兜底没接上，一眼能看出来。

跑测试：

```powershell
uv run pytest
```

```
...................                                                      [100%]
235 passed in 23.97s
```

---

## 8. 生产环境踩坑实录

### 坑 1：循环没有业务刹车，只靠递归上限

**现象**

某个工单卡了 40 分钟，日志里同一个节点被调了几千次，账单暴涨。

**定位方法**

在节点里打计数：

```python
log.info("node_enter", node="fetch", attempts=state.get("attempts"))
```

如果 `attempts` 一路涨到几百，就是这个坑。

**根因**

只依赖 `recursion_limit`（默认 **10007**）。它确实会停，但**停在 10007 步之后**——
那时候钱已经花完了。

**修法**

```diff
- if state.get("attempts", 0) < 10**9:
+ if state.get("attempts", 0) < state.get("max_attempts", 3):
```

**顺手加固**：把 `max_attempts` 放进配置，并按业务设上限（检索重试 2–3 次，
工具调用 1–2 次，LLM 生成 3 次）。

### 坑 2：把异常吞成空结果

**现象**

用户投诉"总是让我补充信息"，但排查发现那段时间下游服务确实在报错。

**定位方法**

看 `fallback` 节点的进入次数。如果一直是 0，而用户确实遇到了失败 —— 说明错误被当成"空结果"了。

**根因**

```python
try:
    docs = await fetch()
except Exception:
    docs = []            # ← 错误消失，变成了"没查到"
```

**修法**

```diff
- except Exception:
-     docs = []
+ except Exception as exc:
+     return {"error": str(exc)}
```

**顺手加固**：在 `RouteState` 里给 `error` 一个明确的类型（而不是 `str | None` 裸用），
并在兜底节点里按错误类型给不同话术（超时 vs 权限 vs 参数错）。

### 坑 3：分支标签写错，跑到不该去的节点

**现象**

明显该走 `clarify` 的问题，却进了 `answer`。

**根因**

```python
def route(state):
    return state.get("intent")      # 可能返回 None 或 "VPN"（大写）
```

返回值不在映射里 → 落不到目标节点。

**修法**

```diff
- return state.get("intent")
+ intent = str(state.get("intent") or "other").lower()
+ return intent if intent in ("vpn", "email") else "other"
```

**顺手加固**：把 router 的返回类型写成 `Literal[...]`（本节就是这么做的），
让类型检查器替你挡住拼写错误。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：循环为什么必须配 <code>max_attempts</code>，递归上限不够吗？</b></summary>

递归上限能停，但默认是 **10007 步**（实测）。跑到那里时，账单和下游早就被打爆了。而且它只能抛异常，说不出"为什么放弃"。业务上限（`max_attempts`）能给出明确的失败语义（"重试 3 次仍查不到，转人工"），这才是用户和运维需要的。

</details>

<details>
<summary><b>Q2：为什么 <code>error</code> 要优先于 <code>docs</code> 判断？</b></summary>

因为"部分成功"比"完全失败"更危险。拿着一半资料给出的答案，用户会以为是完整的。宁可整单失败并明说，也不要交付一个看起来完整、实际有缺口的结论。

</details>

<details>
<summary><b>Q3：条件边和 <code>Command</code> 该怎么选？</b></summary>

默认用条件边：路由是独立纯函数，图结构能从 `add_conditional_edges` 一眼看全，适合评审。只有"算完就知道去哪"、且分支很少时用 `Command`——它能边更新 State 边跳转，读起来连贯，但图的边表里看不到那些跳转。

</details>

### 9.2 动手任务：给"兜底"加分级

**要求**

1. 给 `RouteState.error` 增加类型（`timeout` / `permission` / `unknown`）。
2. `fallback` 节点按错误类型给不同话术：
   - `timeout` → "服务繁忙，请稍后重试"
   - `permission` → "你没有该操作的权限，请联系管理员"
   - `unknown` → "查询失败，已转人工"
3. 路由上区分：`permission` 直接进 `fallback`（不重试）；`timeout` 先重试一次再进 `fallback`。
4. 补 3 个用例覆盖三种错误。

**验收标准**

- 三种错误分别得到三句话术，且 `trace` 里能看到是否发生过重试。
- `permission` 的 `attempts` 恒为 1（不重试）。
- 原有 235 个用例仍全绿。

**提示**：这就是生产里"错误分类"的落点——1.5 在三层异常上做过同样的事，这里是在**图的分支**上再做一次。

### 9.3 拓展挑战

给循环加**时间预算**：State 里放 `deadline`（绝对时间），router 里判断"还剩多少时间"，
超了就放弃。

**思考**：

1. `deadline` 该在哪个节点设置？为什么不能在 router 里设置？
2. 时间预算和 `max_attempts` 同时存在时，谁优先？为什么？
3. 如果图被 checkpointer 恢复（3.4），`deadline` 是"恢复时的当前时间 + 预算"，还是"原来的绝对时间"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **条件边 = router（读 State）+ 映射（标签→节点）**；循环就是把边连回自己，没有特殊语法。
2. **刹车必须业务化**：默认递归上限实测是 **10007 步**，靠它兜底等于让账单先爆。`max_attempts` 才是能解释"为什么放弃"的刹车。
3. **错误边没有魔法**：节点内 try/except → 写 `state.error` → 条件边导到兜底节点。**把故障伪装成"空结果"是最难排查的 bug。**

**已知限制（诚实记录）**

- **错误只有 `str | None`**：没有分类（`timeout`/`permission`/`unknown`），§9.2 是作业。
- **没有时间预算**：只有次数刹车，慢的下游仍可能拖很久。
- **`Command` 版与条件边版逻辑重复**：生产里应该选一个，本节为了对比留了两个。
- **未接 checkpointer**：循环中途崩了仍要重跑（3.4）。

**延伸阅读**

- LangGraph 条件边：https://langchain-ai.github.io/langgraph/concepts/low_level/#conditional-edges
- `Command` 与动态路由：https://langchain-ai.github.io/langgraph/concepts/low_level/#command
- 递归上限与 `recursion_limit`：https://langchain-ai.github.io/langgraph/troubleshooting/errors/GRAPH_RECURSION_LIMIT/

---

## 修订记录

- 2026-11-18 初稿：七步演示与 18 个测试全部实机运行，§7 为真实输出
- 2026-11-18 修正：demo 里误写"默认递归上限 25 步"，实测为 **10007 步**，已改正并写进 §2.4
- 2026-11-18 修正：小结里一处嵌套引号导致语法错误，改用具名引号
