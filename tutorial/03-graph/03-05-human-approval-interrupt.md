---
section: "3.5"
title: "用 interrupt() 做人工审批门禁"
stage: 3
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M3
depends_on: ["3.4"]
produces: ["graph/approval.py"]
verifies: "审批挂起 → 进程退出 → 新进程恢复 → 结果正确"
part: null
updated: "2026-11-18"
---

# 3.5 用 `interrupt()` 做人工审批门禁

## 0. 本节导航

**本节在链路中的位置**：3.1 的"崩法 3：审批无处安放"的正式解法。3.4 给了"状态能活下来"的能力，本节把"人"放进流程里。这也是 M3 的最后一块——3.6 会把这条审批链接进整条工单链路。

**本节你将获得**

1. `interrupt(payload)` 的挂起语义，以及 payload 就是"给人类的审批单"。
2. `Command(resume=...)` 的恢复语义，以及**恢复时节点会从头重跑**这个坑。
3. 三种决定的落地：批准 / 拒绝 / 改写。
4. 超时的正确做法（**interrupt 没有定时器**，超时是恢复时判断的）。
5. 实测：子进程发起审批后退出，新进程从 SQLite 读到审批单并完成审批。

**前置检查清单**

- 3.4 的 `uv run python -m scripts.checkpoint_demo` 能跑通（`interrupt` 依赖 checkpointer）。
- 知道 `next=('node',)` 是什么意思。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

回到 3.1 的实测：

```
未审批时: {'status': 'pending_approval', 'draft': '…重置密码…'}
keys: ['draft', 'status']
→ 想让用户稍后点『批准』再继续，你得自己实现：
  ① 把中间状态存进数据库 ② 建一个 resume 接口 ③ 重启后能找到断点
```

"返回 pending"这个做法的问题不在于"没实现"，而在于**它把三件难事全部推给了你**：

- 存什么？（整个 State？还是几个字段？）
- 存在哪？（怎么保证和业务事务一致？）
- 怎么恢复？（从哪个节点继续？重跑会不会重复副作用？）

而 `interrupt()` 把这三件事变成了框架能力：

```python
answer = interrupt({"ask": "批准吗？", "draft": ...})    # 挂起
# ……进程可以退出，几天后……
await graph.ainvoke(Command(resume="approve"), config)  # 从这一行之后继续
```

**代价是你必须理解它的两个反直觉行为**：

1. **被 resume 的节点会从头重跑**（不是"从 `interrupt` 那一行继续"）
2. **它没有定时器**（"超时"要你自己判断）

---

## 2. 核心概念与架构设计

### 2.1 类比：把公文放进"待签"文件夹

- **`interrupt(payload)`** = 把公文放进"待签"文件夹，盖上"等待处理"的章
- **checkpointer** = 那个文件夹本身（不是你的桌面）
- **`Command(resume=...)`** = 领导签完字，把公文拿回来继续走流程

> **这个类比在哪失效**：真实公文拿回来是"从签字处继续"。
> 而 `interrupt` 恢复时，**整个节点函数会从头执行一遍**，只是这次 `interrupt()` 会**返回**你的答案
> 而不是再次挂起。§6.1 会实测给你看。

### 2.2 挂起的三个事实

实测（本节第 1 步）：

```
挂起了吗: True
next    : ('human_gate',)   ← 停在 human_gate
trace   : ['prepare']       ← 只完成了 prepare
```

三点：

1. **`next` 指向被中断的节点本身**（`human_gate`），不是它的下一个
2. **State 里只有中断之前完成的东西**（`prepare` 写了 trace，`human_gate` 没写）
3. **`ainvoke` 的返回值里有 `__interrupt__`**，含 `Interrupt(value=..., id=...)`

### 2.3 审批单长什么样

```
type         approval_request
ticket_id    T-1001
draft        重置密码的答复（涉及账号操作）
risk         high
expires_at   1794978058（13:00:57 截止）
ask          批准这条答复吗？可回复 approve / reject，或 {action: edit, text: 新答复}
```

**这就是前端要渲染的东西**——一个纯 dict，不含任何业务逻辑。

### 2.4 三道出口

```
        human_gate
             │
     route_decision  ← 先判超时，再判决定
      ┌──────┼──────┐
      ▼      ▼      ▼
  deliver  cancelled  expired
```

**顺序很重要**：先判 `now > expires_at`，再判决定。
所以"人都睡了三天才点批准"会走 `expired` 而不是 `deliver`。

### 2.5 超时：interrupt 没有定时器

这是最容易误解的一点。`interrupt()` **不会**在 N 秒后自动恢复——它只是停在那里。

本节的超时是**恢复时判断**的：

```python
def route_decision(state):
    if time.time() > float(state.get("expires_at") or 0):
        return "expired"
    ...
```

**如果没人来点**，图会永远停在 `human_gate`。要做到"到点自动作废"，你需要：

1. 一个外部定时任务，扫描"超时未审批"的 thread
2. 投递 `Command(resume={"action": "timeout"})` 或直接标记作废

**这一步本节没有实现**，是 §9.2 的作业。

### 2.6 适用范围（边界）

| 场景 | 用 interrupt 吗 |
|---|---|
| 危险操作前要人确认（重置密码、退款） | ✅ |
| 需要人工补充信息（2.6 的 MRTR） | ⚠️ 短流程用 MRTR 更轻，长流程用 interrupt |
| 需要"到点自动通过" | ❌ 要外部调度器 |
| 高频、低风险的确认 | ❌ 打扰人，应该用策略而不是审批 |

---

## 3. 环境与版本

本节**不新增依赖**（`interrupt` / `Command` 在 `langgraph.types`，checkpointer 复用 3.4 的）。

| 组件 | 实测版本 |
|---|---|
| langgraph | `1.2.22` |
| langgraph-checkpoint-sqlite | `3.1.3` |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from langgraph.types import interrupt, Command; print('ok')"
```

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/approval.py` | **主角**：审批图 + 挂起/查询/决定三个接口 |
| `scripts/approval_worker.py` | 子进程：发起审批后退出 |
| `scripts/approval_demo.py` | 五步演示 |
| `tests/test_graph_approval.py` | 11 个断言（含真子进程） |

### 4.1 门禁节点

```python
def human_gate(state: ApprovalState) -> dict[str, Any]:
    """审批门禁：挂起等人类决定。

    ⚠️ 这个节点在恢复时会**从头再执行一遍**。所以：
       - 不要在 interrupt() 之前做任何有副作用的操作
    """
    payload = {
        "type": "approval_request",
        "ticket_id": state.get("ticket_id"),
        "draft": state.get("draft"),
        "risk": state.get("risk", "high"),
        "expires_at": state.get("expires_at"),
        "ask": "批准这条答复吗？可回复 approve / reject，或 {action: edit, text: 新答复}",
    }

    answer = interrupt(payload)      # ← 挂在这里；恢复时这一行"返回"答案

    if isinstance(answer, dict):
        ...
    decision = str(answer or "").strip().lower()
    return {"decision": decision or DECISION_REJECT, "trace": [f"gate:{decision or 'reject'}"]}
```

### 4.2 路由（先判超时）

```python
def route_decision(state: ApprovalState) -> Literal["deliver", "cancelled", "expired"]:
    if time.time() > float(state.get("expires_at") or 0):
        return "expired"
    decision = state.get("decision")
    if decision in (DECISION_APPROVE, DECISION_EDIT):
        return "deliver"
    return "cancelled"
```

### 4.3 三个对外接口

```python
async def start(graph, thread_id, *, ticket_id, draft="", risk="high", timeout_s=300):
    """发起审批：跑到 interrupt 处停下，返回审批单。"""

async def pending(graph, thread_id) -> dict | None:
    """当前是否有等待中的审批；有就返回审批单。"""
    state = await graph.aget_state(thread_config(thread_id))
    if not state.interrupts:
        return None
    return {"payload": state.interrupts[0].value, ...}

async def decide(graph, thread_id, decision):
    """人类做完决定：用 Command(resume=...) 从挂起点继续。"""
    out = await graph.ainvoke(Command(resume=decision), config=thread_config(thread_id))
```

**这三个函数就是"审批系统"的最小 API**：发起 / 查待办 / 提交决定。

### 4.4 运行

```powershell
uv run python -m scripts.approval_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 `interrupt()` 的返回值从哪来

它**不是**函数调用返回，而是一个"暂停点"：

- 第一次执行到它：图挂起，`ainvoke` 返回带 `__interrupt__` 的结果
- 恢复时：节点**从头执行**，再走到这一行，这次它**直接返回** `Command(resume=...)` 里的值

所以 `answer = interrupt(payload)` 这行的语义是："如果有人在等我，就把答案给我；否则挂起"。

### 5.2 为什么节点会从头重跑（**本节最重要的一条**）

实测（第 5 步）：

```
第一次执行：
  进入 gate 第 1 次（副作用已执行 1 次）
人类批准：
  进入 gate 第 2 次（副作用已执行 2 次）
  interrupt 返回: 'approve'

结果：gate 执行了 2 次，副作用发生了 2 次
```

**原因**：LangGraph 的恢复粒度是"节点"，不是"行"。它重新执行整个 `human_gate`，
靠 `interrupt()` 内部的记录知道"这次该返回答案了"。

**后果**：`interrupt()` **之前**的任何副作用（发通知、扣款、写外部系统）都会重复。

**修法**：把副作用放在 `interrupt()` **之后**，或者拆到独立节点里：

```python
# ❌ 错：通知发两次
def gate(state):
    await notify_user(state["ticket_id"])
    answer = interrupt(payload)
    ...

# ✅ 对：副作用在中断之后
def gate(state):
    answer = interrupt(payload)
    return {"decision": answer}

def deliver(state):          # 只有批准后才会走到这里，且只走一次
    await notify_user(...)
```

### 5.3 `state.interrupts` 是什么

`aget_state()` 返回的 `state.interrupts` 是**当前挂起项**的列表。用它做"待办查询"：

```python
state = await graph.aget_state(config)
if state.interrupts:
    payload = state.interrupts[0].value
```

一个 thread 同时可能有多处挂起（并行分支各自的 `interrupt`），所以它是列表不是单个值。

### 5.4 超时为什么放在路由里判

因为 `interrupt()` 没有定时器。放在路由里的好处是：

- **不依赖外部调度**：只要有人来恢复，过期的就自动作废
- **判定集中在一处**：`route_decision` 是唯一的"该不该交付"的判断点

**代价**：没人来恢复，它就一直挂着。真正的"到点作废"需要外部定时器（§9.2）。

### 5.5 `Command(resume=...)` 传什么

可以是任何可 JSON 序列化的东西。本节用了三种：

- 字符串 `"approve"` / `"reject"`
- 字典 `{"action": "edit", "text": "新答复"}`
- （作业里）`{"action": "timeout"}`

**建议用字典**：字符串省事，但加字段时无法向后兼容。

### 5.6 挂起期间状态在哪

**完全在 checkpointer 里**。第 4 步的实测：子进程发起审批后退出（内存全部释放），
新进程打开同一个 SQLite 文件，`pending()` 依然能拿到审批单：

```
新进程读到待审批: True
  审批单的 ticket: T-4001
  next           : ('human_gate',)
审批后: decision=approve
  最终: 已交付：重置密码的答复
```

**这就是 3.4 那节的意义**：没有 checkpointer，`interrupt` 就是个"挂起即丢失"的玩具。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把副作用放在 `interrupt()` 之前

见 §5.2 实测：`gate` 执行 2 次，副作用 2 次。

**用户视角**：点了一次"批准"，收到了两条通知。而且第二条是在他点完之后才发的，
时间戳对不上，排查起来极其费劲。

### 6.2 实验 B：超时不判，直接看决定

把 `route_decision` 里的超时判断删掉：

```
人点了: approve
实际走: prepare → gate:approve → deliver      ← 三天前的答复被交付了
```

**后果**：一条三天前的答复（可能已经过时、甚至已经被别的渠道解决了）被发给了用户。

**这就是"业务时效"必须显式编码的原因**：`interrupt` 只负责"等人"，不负责"等多久算太久"。

### 6.3 实验 C：用 `pending_approval` 状态代替 `interrupt`

这就是 3.1 的做法。对比：

| | 返回 pending | `interrupt()` |
|---|---|---|
| 中间状态 | 你得自己存 | 框架存 |
| 恢复入口 | 你得自己写 | `Command(resume=...)` |
| 从哪继续 | 你得自己算 | `next` 给好了 |
| 重跑副作用 | 你自己控制 | **也会重跑**（同样要小心） |
| 超时 | 你自己判 | 你也得自己判 |

**最后两行说明**：`interrupt` 不是银弹。它替你解决的是"存与恢复"，
**没有**替你解决"副作用幂等"与"业务时效"。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.approval_demo
```

实测（节选）：

```
第 1 步：发起审批 —— 图停在 human_gate
  挂起了吗: True
  next    : ('human_gate',)   ← 停在 human_gate
  trace   : ['prepare']  ← 只完成了 prepare

  交给人类的审批单：
    type         approval_request
    ticket_id    T-1001
    draft        重置密码的答复（涉及账号操作）
    risk         high
    expires_at   1794978058（13:00:57 截止）

第 2 步：三种决定
  批准: decision=approve   最终: 已交付：原始答复   走法: prepare → gate:approve → deliver
  拒绝: decision=reject    最终: 审批未通过，答复已作废  走法: … → cancelled
  改写: decision=edit      最终: 已交付：认证失败请先自行重置密码  走法: … → deliver

第 3 步：超时
  人点了: approve
  实际走: prepare → gate:approve → expired
  最终  : 审批超时，答复已作废。

第 4 步：进程退出
  子进程退出码: 0
    输出: 子进程挂起: next=('human_gate',) expires_at=1794978059
  新进程读到待审批: True
    审批单的 ticket: T-4001
  审批后: decision=approve   最终: 已交付：重置密码的答复

第 5 步：⚠️ 被 resume 的节点会从头重跑
  第一次执行：进入 gate 第 1 次（副作用已执行 1 次）
  人类批准：进入 gate 第 2 次（副作用已执行 2 次）
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 84%]
........................................                                 [100%]
256 passed in 32.53s
```

---

## 8. 生产环境踩坑实录

### 坑 1：节点里少 import，图直接起不来

**现象**

```
NameError: name 'operator' is not defined. Did you forget to import 'operator'?
```

报错位置在 `<string>`，看起来莫名其妙。

**根因**

State 里用了 `Annotated[list[str], operator.add]`，但模块里忘了 `import operator`。
LangGraph 在执行时**动态解析类型注解**（`get_type_hints`），于是报错发生在框架内部。

**修法**

补上 `import operator`。

**顺手加固**：State 定义所在的模块，把所有 reducer 用到的名字（`operator`、`add_messages`、
自己的合并函数）都显式 import——**注解里的名字必须是模块级可见的**。

### 坑 2：子进程输出中文，父进程解码崩

**现象**

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0xb7 in position 14
```

**根因**

`subprocess.run(..., text=True)` 用**系统默认编码**（中文 Windows 上是 gbk）解码子进程输出，
而子进程输出的是 UTF-8。

**修法**

```diff
- subprocess.run(cmd, capture_output=True, text=True)
+ subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
```

**顺手加固**：跨进程通信统一用 UTF-8，不要依赖 `locale`。

### 坑 3：审批挂起后没人管，thread 永久卡住

**现象**

数据库里有一批 `next=('human_gate',)` 的 thread，最老的已经挂了几个月。

**定位方法**

```python
state = await graph.aget_state(config)
if state.interrupts and state.values["expires_at"] < time.time():
    log.warning("approval_stale", thread_id=..., age_hours=...)
```

**根因**

`interrupt` 没有定时器，没人恢复就一直挂着。

**修法**

起一个定时任务扫描过期 thread，投递 `Command(resume={"action": "timeout"})`；
或直接把它们标记为作废并通知发起人。

**顺手加固**：给审批加"提醒"（到期前 1 小时通知审批人），而不仅仅是"到期后作废"。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 `interrupt()` 恢复时节点会从头重跑？带什么后果？</b></summary>

因为 LangGraph 的恢复粒度是"节点"——它重新执行整个节点函数，靠 `interrupt()` 内部的记录知道这次该返回答案。后果是 `interrupt()` **之前**的副作用会重复执行（实测 gate 进入 2 次、副作用 2 次）。所以副作用必须放在 `interrupt()` 之后，或拆到独立节点。

</details>

<details>
<summary><b>Q2：`interrupt` 有超时吗？没有的话怎么做？</b></summary>

没有。它只是停在那里，不会自己醒。本节的超时是**恢复时判断**的（`route_decision` 里先比 `expires_at`）。真正的"到点自动作废"需要外部定时任务扫描过期 thread 并投递 resume。

</details>

<details>
<summary><b>Q3：挂起期间状态存在哪？为什么必须有 checkpointer？</b></summary>

存在 checkpointer 里（本节用 SQLite）。没有它，`interrupt` 只会挂起然后丢失——进程一退出就无从恢复，"审批"就变成了"同步阻塞等人"。第 4 步实测：子进程退出后，新进程仍能从 SQLite 读到审批单并完成审批。

</details>

### 9.2 动手任务：给审批加"超时自动作废"

**要求**

1. 写一个 `sweep_timeouts(graph, now)` 函数：扫描所有挂起的 thread，对已过期的投递
   `Command(resume={"action": "timeout"})`。
2. 给 `route_decision` 增加 `timeout` 分支（与 `expired` 区分：`expired` 是"过期后才点"，
   `timeout` 是"过期且没人点"）。
3. 补 3 个用例：未过期不处理、过期被作废、作废后 `pending()` 返回 `None`。
4. 加一条日志：作废时记录 `ticket_id`、`expires_at`、`swept_at`。

**验收标准**

- `sweep_timeouts` 只处理 `next` 非空且 `expires_at < now` 的 thread。
- 作废后再次 `decide` 不会重复交付。
- 原有 256 个用例仍全绿。

**提示**：`state.interrupts` 为空但 `next` 非空的 thread 属于"损坏状态"，要单独告警而不是静默跳过。

### 9.3 拓展挑战

把审批做成**多人会签**：需要两个不同角色都批准才交付。

**思考**：

1. 两个 `interrupt` 该放在同一个节点，还是两个节点？
2. 第一个人批准后、第二个人还没批时，`next` 是什么？
3. 如果有人拒绝了，怎么让"已经批过"的那个人知道？（提示：State 里要有 `decisions` 列表）

---

## 10. 本节小结与延伸

**三点回顾**

1. **`interrupt(payload)` 挂起、`Command(resume=...)` 恢复**，中间靠 checkpointer 存活。实测：子进程退出后新进程能接着审批。
2. **被 resume 的节点从头重跑**：实测 gate 进入 2 次、副作用 2 次。**副作用必须放在 `interrupt` 之后**。
3. **`interrupt` 没有定时器**：超时是恢复时判断的；"到点自动作废"要外部调度。

**已知限制（诚实记录）**

- **没有超时扫描器**：没人恢复就一直挂着（§9.2 是作业）。
- **没有提醒机制**：到期前不通知审批人。
- **单人会签**：不支持"两个角色都批"（§9.3）。
- **审批单没有权限校验**：任何能调 `decide()` 的人都能批；生产里必须校验"这个人有没有审批权"（2.6 的 `admin:approve`）。

**延伸阅读**

- LangGraph `interrupt`：https://langchain-ai.github.io/langgraph/concepts/human-in-the-loop/
- `Command` 与 resume：https://langchain-ai.github.io/langgraph/reference/types/#langgraph.types.Command
- 人机回环的四种模式：https://langchain-ai.github.io/langgraph/concepts/human-in-the-loop/#design-patterns

---

## 修订记录

- 2026-11-18 初稿：五步演示与 11 个测试全部实机运行；子进程挂起+新进程恢复通过
- 2026-11-18 修正：`approval.py` 漏 `import operator` 导致 `NameError: name 'operator' is not defined`（LangGraph 动态解析注解时触发）
- 2026-11-18 修正：子进程输出中文时 `subprocess.run(text=True)` 用 gbk 解码报错，统一加 `encoding="utf-8"`
- 2026-11-18 补充：新增"gate 重跑"实测（副作用执行 2 次），把"interrupt 前不能放副作用"变成可验证结论
