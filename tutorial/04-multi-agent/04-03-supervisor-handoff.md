---
section: "4.3"
title: "Supervisor 与 Handoff：两种多 Agent 拓扑"
stage: 4
status: done
depth: L2
difficulty: 4
minutes: 110
milestone: M4
depends_on: ["4.2"]
produces: ["multiagent/topologies.py"]
verifies: "同一任务在两种拓扑下都能完成，且能说清差异"
part: null
updated: "2026-11-18"
---

# 4.3 Supervisor 与 Handoff：两种多 Agent 拓扑

## 0. 本节导航

**本节在链路中的位置**：4.1 论证了"先别拆"，4.2 跨进程了。本节假设你**已经决定要拆**，那下一个问题是：**拆成什么形状？**

两种主流拓扑，本节点对点量清楚：**轮次差 2 倍、trace 差 2 倍、边表可见性完全不同**。

**本节你将获得**

1. **supervisor（中心调度）**：worker 干完回中心，由中心派下一跳。
2. **handoff（对等转移）**：worker 用 `Command(goto=...)` 直接交给下一个，没有中心。
3. 实测：同一任务集，supervisor **每任务 10 轮**，handoff **每任务 5 轮**（**2.0 倍**）。
4. handoff 的阴暗面：**踢皮球**（实测 `write` 被访问 3 次）以及必须自带的止损。
5. 一个我踩到的死循环：**用"字段是否为空"推断进度**，无资料的任务会永远转。

**前置检查清单**

- 4.2 的 `uv run python -m scripts.a2a_demo` 能跑通。
- 3.3 的 `Command(goto=...)` 已理解（本节 handoff 完全靠它）。

**难度**：★★★★☆ ｜ **预计时长**：110 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

4.1 的多 Agent 其实只有一种形状：**supervisor 轮转**（中心派活、worker 回报）。
但真实系统里还有另一种常见形状：**对等转移**——A 干完直接把活交给 B，没有中心。

两者不是"风格差异"，而是**成本结构不同**：

| | supervisor | handoff |
|---|---|---|
| 一次任务要几跳 | 每个 worker 都要绕回中心 → **约 2 倍** | 每个 worker 只跳一次 |
| 谁掌握全局 | 中心 | **没有人** |
| 出问题怎么发现 | 中心能计数 | 各自自救 |

**选错拓扑的代价**：用 supervisor 做 3 跳的短链路，白付一倍调度成本；
用 handoff 做 6 个角色的长链路，**踢皮球没人管**。

---

## 2. 核心概念与架构设计

### 2.1 类比：公司审批 vs 接力赛

- **supervisor = 公司审批流**：每个人办完都要交回"流程中心"，中心再分给下一个人。
  好处是中心知道全局（谁办到哪了、卡了多久）；代价是**每次都要回到中心**。
- **handoff = 接力赛**：跑完直接把棒子交给下一个。少一次折返，但**没人知道全场进度**，
  而且如果两个人互相传棒（你办不了给我、我办不了给你），**没人会喊停**。

> **这个类比在哪失效**：接力赛的下一棒是**赛前定好**的；
> handoff 的下一棒是**运行时由当前 agent 决定**的（`Command(goto=...)`）——
> 这正是它灵活、也正是它不可预测的原因。

### 2.2 supervisor 的形状

```
        ┌────────────┐
        │ supervisor │◄───────────┐
        └─────┬──────┘            │
   ┌──────┬───┴───┬──────┐         │
   ▼      ▼       ▼      ▼         │
triage research write review ──────┘
                     │
                     ▼
                 finalize → END
```

**实测边表**（节选）：

```
('triage', 'supervisor'), ('research', 'supervisor'),
('write', 'supervisor'), ('review', 'supervisor'),
('supervisor', 'triage'), ('supervisor', 'research'), …
```

**每个 worker 都有一条回中心的边。** 图上一眼可见。

### 2.3 handoff 的形状

```
triage ──► research ──► write ──► review ──► END
                          ▲          │
                          └──────────┘   （不合格就打回）
```

**实测边表**：`[]` —— 一条都没有！

因为所有跳转都写在 `Command(goto=...)` 里，`get_graph()` 看不到它们。

**这是 handoff 最重要的特征（也是它最大的缺点）**：
**图的不变量从"边表"变成了"每个节点里的 goto 分支"**，评审时你要把每个节点都读一遍。

### 2.4 实测数字

同一批 3 个任务，两种拓扑都 3/3 成功：

| 指标 | supervisor | handoff | 差异 |
|---|---|---|---|
| 每任务轮次 | **10.0** | **5.0** | **2.0 倍** |
| 每任务交接说明 | 5.0 | 5.0 | 相同 |
| 每任务 trace 长度 | **10.0** | **5.0** | 2.0 倍 |

**为什么正好 2 倍**：supervisor 每派一次活，worker 干完都要回中心；
5 个 worker → 5 次派活 + 5 次回中心 = 10。handoff 只有 5 次前后传递。

**这多出来的一倍，就是"全局可见"的票价。**

### 2.5 handoff 的踢皮球（实测）

任务"打印机坏了"（知识库没有这类资料）：

```
trace: triage → researcher → writer → reviewer:reject → writer → reviewer:reject → writer:abort
visits: {'triage': 1, 'research': 1, 'write': 3, 'review': 2}
最终: 来回改写超过 2 次，已止损转人工。
```

**review 打回 write，write 又交回 review**——如果没有止损，就会一直转
（§6.2 实测：把 guard 放开，6 步内就撞 `GraphRecursionError`）。

**handoff 拓扑必须自己管循环**，因为**没有中心节点帮你数**。
本节的止损条件是 `visits['write'] > 2`，写在前 `write` 节点自己身上。

### 2.6 适用范围（边界）

| 场景 | 拓扑 |
|---|---|
| 角色 > 3、需要统一策略/审计 | supervisor |
| 链路 2–3 跳、角色对等 | handoff |
| 需要"谁卡住了"的全局视图 | supervisor |
| 需要低延迟、少一跳 | handoff |
| 会被别的团队读图做评审 | supervisor（边表完整） |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 |
|---|---|
| langgraph | `1.2.22`（`Command` / `add_conditional_edges`） |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from servicedesk.multiagent.topologies import compare_topologies; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/multiagent/topologies.py` | **主角**：两种拓扑 + 跑分 |
| `scripts/topologies_demo.py` | 五步演示 |
| `tests/test_topologies.py` | 16 个断言 |

### 4.1 supervisor：显式阶段路由

```python
_STAGE_NEXT = {
    "": "triage",
    "triaged": "research",
    "researched": "write",
    "written": "review",
    "reviewed": "finalize",
}


def _route_supervisor(state: TopoState) -> str:
    """按**显式阶段**路由。

    教训（本节踩过）：原先用"字段是否为空"推断进度，结果"无资料"的任务
    `docs` 一直为空，supervisor 就一直派 research —— 死循环。
    进度必须是**显式状态**，不能靠数据形状猜。
    """
    return _STAGE_NEXT.get(state.get("stage", ""), "finalize")
```

### 4.2 handoff：`Command` 直接跳

```python
def _ho_triage(state: TopoState) -> Command:
    intent = _classify(state.get("title", ""), state.get("description", ""))
    visits = _bump(state, "triage")
    return Command(
        goto="research",                 # ← 自己决定下一个是谁
        update={"intent": intent, "visits": visits, ...},
    )
```

### 4.3 handoff 的止损

```python
    # 防"踢皮球"：同一个 agent 被访问超过 2 次就止损
    if visits.get("write", 0) > 2:
        return Command(
            goto=END,
            update={"final": "来回改写超过 2 次，已止损转人工。", "trace": ["writer:abort"]},
        )
```

**这个判断只能写在自己身上**——没有中心会替你做。

### 4.4 运行

```powershell
uv run python -m scripts.topologies_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 supervisor 的轮次正好翻倍

因为它**每跳都要插一次中心**：

```
supervisor → triage → supervisor → research → supervisor → write → …
```

5 个 worker = 5 次"派活" + 5 次"回报" = 10 轮。

**这多出来的调度轮次不是浪费**：它换来"中心随时知道全局"。但如果你的链路只有 2 跳，
这 2 倍就是纯成本——**这就是 4.1 说"先别拆"的原因之一**。

### 5.2 为什么 handoff 的边表是空的

`add_conditional_edges` 会把分支写进图；`Command(goto=...)` **不会**。

实测：

```
handoff 节点: ['triage', 'research', 'write', 'review']
handoff 边  : []
```

**这意味着**：

- `get_graph(xray=True)` 看不到实际跳转
- 静态分析工具（查循环、查不可达节点）**全部失效**
- 新人读代码必须逐个节点看 `goto`

**代价是真实的**。所以本节在 §2.6 里把"会被别人评审"划给了 supervisor。

### 5.3 进度为什么要用显式 `stage`

这是我本节踩的最大的坑（§8.1）。

用"字段是否为空"推断进度看起来聪明：

```python
if not state.get("docs"):
    target = "research"
```

但"无资料"的任务 `docs` **永远为空** → supervisor 永远派 research → 死循环，
直到 `GraphRecursionError`。

**修法**：每个 worker 写显式阶段，中心只看阶段。

**这条规则可以推广**：**别用"数据形状"当状态机。** 空列表可能意味着"还没查"，
也可能意味着"查了但没有"——两者语义完全不同。

### 5.4 handoff 的止损为什么写在 `write` 里

因为**只有 `write` 知道自己被反复调用了**（每次进来 `visits['write'] += 1`）。

写在 `review` 里行不行？可以，但 `review` 看到的是"我打回了 2 次"，
它不知道 `write` 是不是每次都在做同样的事。**把止损放在"被反复调用的那一方"更准确。**

### 5.5 交接说明（`notes`）为什么两种拓扑相同

因为本节让**每个 worker 干完都写一条 notes**，与拓扑无关。实测两种都是 5.0 条/任务。

这说明：**`notes` 记的是"业务动作"，`trace` 记的是"调度路径"。**
看轮次差异要看 `trace`，不是 `notes`。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：用字段形状推断进度 → 死循环

把 `_route_supervisor` 改回"看字段是否为空"：

```
GraphRecursionError: Recursion limit of 40 reached without hitting a stop condition.
```

**任务集里只要有一个"查了但没有资料"的用例，就会触发。**
而这类用例在真实系统里非常常见（冷门问题、新上线业务）。

**修法**：显式 `stage`（§5.3）。

### 6.2 实验 B：去掉 handoff 的止损

```
visits: {'write': 3, 'review': 2}
→ 放开 guard 后：GraphRecursionError（recursion_limit=6 时）
```

**注意这个后果和 4.1 的"多 Agent 延迟"不是一回事**：延迟只是慢，
**踢皮球是"永远不结束"**——任务永远不会 `completed`，用户永远在等。

### 6.3 实验 C：把 handoff 的跳转改成条件边

如果硬要把 handoff 用 `add_conditional_edges` 表达，你会得到什么？

- 每条跳转都要一个 router 函数
- `review → write` 和 `write → review` 要写成两条条件边
- 实际上**它就变成了 supervisor 的另一种写法**（只是中心隐含在 router 里）

**结论**：拓扑不是"画法"，而是"控制权在谁手里"。
**把控制权交给每个节点（handoff），换来少一跳；交给中心（supervisor），换来全局可见。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.topologies_demo
```

实测（节选）：

```
第 1 步：两种拓扑长什么样
  supervisor 节点: ['supervisor', 'triage', 'research', 'write', 'review', 'finalize']
  supervisor 边  : [('research', 'supervisor'), ('review', 'supervisor'),
                   ('supervisor', 'finalize'), ('supervisor', 'research'), …]
  handoff    节点: ['triage', 'research', 'write', 'review']
  handoff    边  : []

第 2 步：同一批 3 个任务
  指标                  supervisor          handoff
  成功                  3/3                 3/3
  每任务轮次            10.0                5.0
  每任务 trace 长度     10.0                5.0

  轮次比: supervisor 是 handoff 的 2.0 倍

第 3 步：逐任务看 trace
  supervisor trace (10 条):
    supervisor->triage → triage → supervisor->research → researcher →
    supervisor->write → writer → supervisor->review → reviewer →
    supervisor->finalize → finalize

  handoff trace (4 条):
    triage → researcher → writer → reviewer:ok

第 4 步：handoff 的踢皮球与止损
  trace: triage → researcher → writer → reviewer:reject → writer →
         reviewer:reject → writer:abort
  visits: {'triage': 1, 'research': 1, 'write': 3, 'review': 2}
  最终: 来回改写超过 2 次，已止损转人工。
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 90%]
...............................                                          [100%]
319 passed in 43.15s
```

---

## 8. 生产环境踩坑实录

### 坑 1：用"字段是否为空"当状态机，无资料任务死循环

**现象**

```
GraphRecursionError: Recursion limit of 40 reached without hitting a stop condition.
```

而且**只有部分任务**会这样（冷门问题、知识库没覆盖的）。

**定位方法**

打一行进度日志：

```python
log.info("supervisor_route", stage=state.get("stage"),
         docs=len(state.get("docs") or []), target=target)
```

如果 `target` 一直是同一个，就是这个坑。

**根因**

`if not state.get("docs"): target = "research"` —— "还没查"和"查了但没有"不可区分。

**修法**

显式 `stage` 字段，每个 worker 写自己的阶段。

**顺手加固**：给"无资料"的任务写一条测试（本节 `test_missing_stage_does_not_loop`）。

### 坑 2：handoff 踢皮球，任务永远不结束

**现象**

某个工单的处理记录里，两个角色互相打回十几次，最后由超时兜底。

**定位方法**

统计每个 agent 的访问次数：

```python
print(out["visits"])   # {'write': 7, 'review': 6}
```

**根因**

handoff 拓扑没有中心节点，**没有人发现循环**。

**修法**

在"被反复调用的一方"加止损（本节 `visits['write'] > 2`），并把"已试过什么"写进 state。

**顺手加固**：把 `visits` 进监控——某个 agent 的访问次数 P99 突然升高，就是踢皮球的信号。

### 坑 3：以为 handoff 更"现代化"就选了它

**现象**

架构评审时发现没人能画出这张图——因为跳转全在 `Command` 里。

**根因**

把拓扑当成风格选择，而不是**成本结构选择**。

**修法**

按 §2.6 的表选：角色多/要审计 → supervisor；链路短/角色对等 → handoff。

**顺手加固**：handoff 的每个节点都要在 docstring 里写清"什么情况下 goto 到哪"，
否则图就没法评审。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 supervisor 的轮次大约是 handoff 的 2 倍？</b></summary>

因为每次派活后 worker 都要回中心再走下一跳：5 个 worker = 5 次派活 + 5 次回报 = 10 轮。handoff 只有 5 次前后传递。这多出的一倍就是"中心随时掌握全局"的代价。

</details>

<details>
<summary><b>Q2：handoff 的边表为什么是空的？这意味着什么？</b></summary>

因为跳转写在 `Command(goto=...)` 里，不经过 `add_conditional_edges`，所以 `get_graph()` 看不到。后果：静态分析失效、新人要逐节点读 goto、评审时无法一眼看全流程。这是 handoff 最大的工程代价。

</details>

<details>
<summary><b>Q3：为什么用"字段是否为空"推断进度会死循环？</b></summary>

因为"还没查"和"查了但没有"都表现为空列表。无资料的任务 `docs` 永远为空，中心就一直派 `research`。修法是显式 `stage` 字段——**别拿数据形状当状态机**。

</details>

### 9.2 动手任务：给 handoff 加"记忆"

**要求**

1. 在 `TopoState` 里加 `attempted: list[str]`（记录已试过的方案）。
2. `write` 每次被调用时，先检查"这次和上次试的是不是同一条路"，是则直接 goto END 并说明原因。
3. 对比：加记忆前 vs 加记忆后，T-3 的 `visits['write']` 从 3 降到 2。
4. 补 3 个用例：首次不触发、第二次触发、记忆为空时正常跑。

**验收标准**

- T-3 的 `visits['write'] == 2`（比现在的 3 少一次无效往返）。
- 有资料的任务行为不变（3/3 成功）。
- 原有 319 个用例仍全绿。

**提示**：这就是"用记忆代替计数"——计数只知道"来了几次"，记忆知道"试过什么"。

### 9.3 拓展挑战

实现第三种拓扑：**层级（hierarchical）**——supervisor 之上再有一个 meta-supervisor，管两个小组。

**思考**：

1. 层级拓扑的轮次是 supervisor 的几倍？（先算再测）
2. 什么情况下层级真的必要？（提示：角色 > 7 时，单个中心的上下文会爆炸）
3. 子图的 `interrupt` 在层级拓扑里怎么冒泡？（3.7 的经验）

---

## 10. 本节小结与延伸

**三点回顾**

1. **supervisor 每跳必回中心**，轮次约 2 倍（实测 10 vs 5），换来全局可见与循环可控。
2. **handoff 少一跳但边表为空**（实测 `[]`），跳转全靠 `Command(goto=...)`；代价是评审与静态分析失效。
3. **handoff 必须自带止损**：实测 T-3 的 `write` 被访问 3 次，靠自己 `visits > 2` 止损；没有它就会踢到递归上限。

**已知限制（诚实记录）**

- **无真实 LLM**：角色决策是规则式的，测不出"模型会不会踢皮球"。
- **止损是计数式**：不知道"试过什么"，§9.2 的作业是加记忆。
- **没有层级拓扑**（§9.3）。
- **没有延迟/成本指标**：本节只量轮次与 trace（4.1 量过延迟）。

**延伸阅读**

- LangGraph 多 Agent 拓扑：https://langchain-ai.github.io/langgraph/concepts/multi_agent/
- `Command` 与 handoff：https://langchain-ai.github.io/langgraph/concepts/low_level/#command
- Anthropic《Building effective agents》（orchestrator-workers 模式）：https://www.anthropic.com/research/building-effective-agents

---

## 修订记录

- 2026-11-18 初稿：两种拓扑 + 16 个测试全部实机运行；轮次 2.0 倍、踢皮球止损均为实测
- 2026-11-18 修正：supervisor 初版用"字段是否为空"推断进度，导致无资料任务死循环（`GraphRecursionError`）；改为显式 `stage` 路由，并留一条回归测试
- 2026-11-18 修正：`TopoState.visits` 的注解里留了一处无效的三元表达式，清理为普通 `dict[str, int]`
