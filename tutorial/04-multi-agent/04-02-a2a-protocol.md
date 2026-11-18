---
section: "4.2"
title: "用 A2A 协议做跨 Agent 协作"
stage: 4
status: done
depth: L2
difficulty: 4
minutes: 110
milestone: M4
depends_on: ["4.1"]
produces: ["a2a/server.py", "a2a/client.py"]
verifies: "两个独立进程通过 A2A 协作完成一次跨域请求"
part: null
updated: "2026-11-18"
---

# 4.2 用 A2A 协议做跨 Agent 协作

> **本节含一处对大纲的更正**：大纲写的是"用 `tasks/send` 提交长任务"，
> 而从 A2A v1.0 起，方法名已是 **`message/send`**，`tasks/send` 是被取代的旧名。
> 本节按 v1.0 实现，并把旧名保留为兼容别名（客户端写旧名也能调通）。

## 0. 本节导航

**本节在链路中的位置**：4.1 讲的是**同一个进程内**的多 Agent（supervisor + worker）。本节跨出去：**两个独立进程、可能是两个团队、甚至两家公司的 Agent** 怎么协作。

**本节你将获得**

1. Agent Card 的发现流程——"派活之前先看清对方会什么"。
2. `message/send` 的**先接单后干活**语义：返回耗时实测 **0ms**，而完成要 ~450ms。
3. 6 个任务状态的流转与**终态保护**。
4. `input-required` 的正确用法，以及**忘记带 `taskId` 会发生什么**（实测：原任务永远挂着）。
5. A2A 与 MCP 的边界表——一句话：**MCP 让 Agent 用手，A2A 让 Agent 找人。**

**前置检查清单**

- 4.1 的 `uv run python -m scripts.single_vs_multi_demo` 能跑通。
- 2.x 的 HTTP/JSON-RPC 经验（A2A 也是 JSON-RPC 2.0）。

**难度**：★★★★☆ ｜ **预计时长**：110 分钟 ｜ **本节费用**：**¥0**（本地回环）

---

## 1. 为什么需要它（痛点先行）

4.1 的 supervisor 模式有个隐含前提：**所有 worker 都在我的进程里，用我的 state**。

一旦要跨出去，这个前提就崩了：

- 对方的 Agent 在我**另一个服务**里（不同仓库、不同部署）
- 对方可能是**合作方**的 Agent，我既不能把 state 塞给它，也读不到它的内部
- 一次协作可能**跑几分钟**，不能让我这边一直阻塞等

这时候你需要的不是"更多节点"，而是一套**协议**：

| 问题 | 单进程多 Agent | 跨 Agent 协作 |
|---|---|---|
| 对方会什么 | 我知道代码 | 得**发现**（Agent Card） |
| 怎么派活 | 直接调函数 | 发消息 + **接单号** |
| 任务长怎么办 | await 等 | 先接单，**轮询** |
| 中间要补信息 | 改 state | `input-required` + **带 taskId 续谈** |
| 怎么取消 | `task.cancel()` | `tasks/cancel` |

**A2A（Agent2Agent）就是这套协议**，现在是 Linux Foundation 项目。

---

## 2. 核心概念与架构设计

### 2.1 类比：把活外包出去

- **Agent Card = 对方公司的能力简介**（做什么、案例、怎么联系）
- **`message/send` = 下订单**（对方给你一个**订单号**，不可能是"你把活干完我才回话"）
- **`tasks/get` = 查订单进度**
- **`input-required` = 对方说"你需求没写清楚，补一下"**
- **`tasks/cancel` = 撤单**

> **这个类比在哪失效**：外包可以打电话催；A2A 里你要是**不定时轮询**，
> 就永远不知道对方已经交付了——**协议不推给你，你就得自己来取**（除非用流式/push）。

### 2.2 四个端点

```
GET  /.well-known/agent-card.json     ← 发现（公开元数据，不产生任务）
POST /                                ← JSON-RPC 统一入口
       message/send                   ← 派活（旧名 tasks/send 兼容）
       tasks/get                      ← 查状态
       tasks/cancel                   ← 撤单
       tasks/list                     ← 列清单（v1.0 有；部分实现没有）
```

### 2.3 任务状态机（本节实现 6 个）

```
submitted ──► working ──► completed
    │            │
    │            ├──► canceled      （人工撤单）
    │            └──► failed        （内部错误）
    └──► input-required ──（补信息）──► working
```

**终态：`completed` / `canceled` / `failed` / `rejected`。**
**终态任务不能再续**——这是本节实测的一条硬保护（§6.2）。

### 2.4 Agent Card 长什么样

实测返回（节选）：

```json
{
  "name": "servicedesk-agent",
  "description": "IT 服务台 Agent：查设备、查权限、检索知识库、建工单。",
  "version": "0.1.0",
  "url": "http://127.0.0.1:51929",
  "capabilities": {"streaming": true, "pushNotifications": false,
                   "stateTransitionHistory": true},
  "defaultInputModes": ["text/plain"],
  "skills": [{"id": "it_support", "name": "IT 支持",
              "description": "根据症状描述给出排查步骤，必要时要求补充设备信息。",
              "tags": ["it", "vpn", "email"],
              "examples": ["NB-1024 的 VPN 连不上怎么办"]}]
}
```

**关键点**：

- **`skills` 是发现的核心**：对方据此决定"这活派不派给你"
- **`capabilities` 声明能力**：支不支持流式、能不能推通知
- **发现不产生任何任务**——所以客户端可以放心地"先看一眼"

### 2.5 A2A 与 MCP 的边界

| 维度 | A2A | MCP |
|---|---|---|
| 解决什么 | Agent ↔ Agent | Agent ↔ 工具/数据 |
| 对端是谁 | 另一个会思考的 Agent | 一个可调用的能力 |
| 交互形态 | 派任务（长、异步、可取消） | 调函数（短、同步、幂等） |
| 状态 | 任务状态机 | 单次请求-响应，无任务概念 |
| 发现 | Agent Card（能力/技能/示例） | `tools/list`（名字/schema） |

> **记忆方法**：**MCP 让 Agent 用手；A2A 让 Agent 找人。**
>
> 一个 Agent 完全可以**同时是 MCP 客户端和 A2A 服务端**——2.x 造的服务台 Agent，
> 本节就用 A2A 把它暴露出去，而它内部仍然通过 MCP 调工具。

### 2.6 适用范围（边界）

| 场景 | 选择 |
|---|---|
| 同一进程内的多角色协作 | 4.1 的 supervisor（更简单） |
| 跨服务、跨团队、跨厂商 | ✅ A2A |
| 任务要跑几分钟、要能取消 | ✅ A2A |
| 只是调一个工具 | ❌ 用 MCP，别套 A2A |
| 对方 Agent 不对外暴露能力 | ❌ 没 Card 就没法发现 |

---

## 3. 环境与版本

本节**不新增依赖**（复用 `httpx` + 标准库）。

| 组件 | 说明 |
|---|---|
| A2A 规范 | v1.0（Linux Foundation）；Agent Card 在 `/.well-known/agent-card.json` |
| JSON-RPC | `2.0`；方法名 `message/send`、`tasks/get`、`tasks/cancel`、`tasks/list` |
| Python | `3.12.13` |

**规范出处**（写作时核对过）：

- Agent Card 路径与 JSON-RPC 方法名：[A2A v1.0 对齐清单](https://github.com/ariffazil/aaa/blob/fcc2d2f0abe2f3e400ccd63d5a5269a9eec66226/a2a-server/ALIGNMENT_MD.md)
- `message/send` 取代 `tasks/send`：[A2A 协议最新特性](https://github.com/vishalmysore/a2ajava/blob/107dda885b204c8c16e9ae39fd4e9c68b4a6c904/tutorial/12_A2A_Latest_Protocol_Features.md)
- 终态不能重启：[A2A v0.3 RPC 方法](https://github.com/mckinsey/agents-at-scale-ark/blob/main/.claude/skills/a2a-protocol/references/v0.3/spec-07-rpc-methods.md)

> ⚠️ **待核实**：`configuration`（如 `blocking`、`returnImmediately`）与 `artifact` 的完整字段集
> 在 v1.0 仍在演进。本节只实现任务流所需的最小字段，落地前请对照官方 spec。

---

## 4. 代码实战（5 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/a2a/server.py` | **主角**：Agent Card + JSON-RPC + 任务状态机 |
| `src/servicedesk/a2a/client.py` | 发现 / 派活 / 轮询 / 取消 |
| `scripts/a2a_worker.py` | 子进程入口（真·独立进程） |
| `scripts/a2a_demo.py` | 六步演示（含 A2A vs MCP 对照） |
| `tests/test_a2a.py` | 14 个断言 |

### 4.1 Agent Card

```python
def build_agent_card(*, base_url: str, name: str = "servicedesk-agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "IT 服务台 Agent：查设备、查权限、检索知识库、建工单。",
        "version": "0.1.0",
        "url": base_url,
        "capabilities": {"streaming": True, "pushNotifications": False,
                         "stateTransitionHistory": True},
        "skills": [{"id": "it_support", "name": "IT 支持", ...}],
    }
```

### 4.2 派活：先接单，再干活

```python
    if method in ("message/send", "tasks/send"):
        message = params.get("message") or params
        text = _text_of(message)

        task_id = message.get("taskId")
        if task_id:
            task = store.get(task_id)          # 续谈：把消息并进同一个任务
            ...
        else:
            task = store.create(message.get("contextId") or f"ctx_...", text)

        threading.Thread(target=_run_task, args=(store, task, text), daemon=True).start()
        return _result(request_id, task.as_dict())      # ← 立刻返回，不等活干完
```

**`threading.Thread` 那一行就是"先接单后干活"**。真实系统里这里是任务队列（Celery / SQS）。

### 4.3 状态推进与 `input-required`

```python
def _run_task(store, task, text):
    time.sleep(0.15)
    if task.state == STATE_CANCELED:
        return

    if not any(_mentions_device(item) for item in _history_texts(task)):
        task.state = STATE_INPUT_REQUIRED
        task.status_text = "请提供设备主机名（形如 NB-1024），我才能查资产与权限。"
        return
    ...
```

### 4.4 轮询

```python
async def wait(self, task_id, *, client, timeout_s=20.0, interval_s=0.1,
               on_state=None, stop_on_input_required=True, known_state=None):
    last_state = known_state or ""
    while time.monotonic() < deadline:
        task = await self.get(task_id, client=client)
        state = task["status"]["state"]
        if state != last_state:
            last_state = state
            if on_state:
                on_state(task)
        if state in TERMINAL_STATES or (stop_on_input_required and state == "input-required"):
            return task
        await asyncio.sleep(interval_s)
```

### 4.5 运行

```powershell
uv run python -m scripts.a2a_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 `message/send` 必须立刻返回

实测：

```
state     : submitted
返回耗时  : 0ms
```

而任务真正完成要 **~450ms**（0.15s 等 + 0.25s 干活，再加轮询间隔）。

**如果同步等**：调用方那 450ms 什么都干不了；如果任务要 5 分钟，连接就会超时断开。
**长任务的正确形态是"接单 + 查进度"，不是"一次 HTTP 等到底"。**

### 5.2 为什么必须有 `tasks/get`

因为 A2A **默认不推送**。对方完成时不会通知你（除非协商了 push notification 或流式）。

**所以客户端必须自己轮询**。这也解释了为什么协议要单独定义一个"查任务"的方法——
它不是一个可选的便利接口，而是**长任务能工作的前提**。

### 5.3 `input-required` 与 `taskId`

实测时间线（补信息带 `taskId`）：

```
[ 0.01s] submitted       任务已接收
[ 0.23s] input-required  请提供设备主机名（形如 NB-1024）…
[ 0.45s] working         正在检索知识库与设备信息…
[ 0.67s] completed       已完成
```

**补信息时传 `message.taskId`**，服务端据此把这条消息并进同一个任务。
`history` 会变成 3 条（用户两条 + Agent 一条）。

**对比**：如果不传 `taskId`（§6.1 实测），会**新建一个任务**，原来那个永远停在 `input-required`。

### 5.4 终态保护

```python
if task.state in TERMINAL_STATES:
    return _error(request_id, -32600, "终态任务不能继续")
```

实测：

```
再取消一次: A2A 错误：{'code': -32600, 'message': '任务已是终态：canceled'}
```

**为什么必须保护**：否则一个"已完成"的任务可以被继续追加消息，产生第二个 `completed`——
**两次交付，用户收到两条**。这跟 3.5 的"被 resume 的节点重跑"是同一类风险，只是层级不同。

### 5.5 `contextId` 是什么

**一次对话的标识**，跨任务复用。本节的实现很简：新任务没给 `contextId` 就生成一个。

**它的价值**：同一个 `contextId` 下的多个任务，对端可以当作同一段对话来理解上下文。
（⚠️ 规范允许 Agent 自行过期/清理 context，客户端不能假设它永久有效。）

### 5.6 为什么用子进程跑 Server

因为"两个独立进程协作"是本节验收点。同进程内起 server 测不出：

- 任务状态存在**服务端内存**里——客户端重启不影响它
- 客户端与服务端之间**只有 HTTP**，没有共享内存

`scripts/a2a_worker.py` 就是那个独立进程；demo 用 `Popen` 拉起它、等 `READY` 行、结束后 `terminate`。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：补信息时忘了带 `taskId`

实测：

```
原任务: task_56ffa5696e18 -> input-required
新任务: task_0e7b5ac57508 -> completed
任务总数: 2
```

**后果**：

- 用户以为"我补了信息，事情在办"
- 服务端有**两个任务**：一个永远挂着，一个莫名其妙完成了
- 对账时你会发现"未完成任务"里堆满了这类僵尸

**修法**：客户端必须记住 `taskId` 并在续谈时带上。
**工程上的加固**：服务端在 `input-required` 的状态消息里**回显 taskId**，让客户端不容易丢。

### 6.2 实验 B：让终态任务可继续

去掉终态保护，会发生什么：

- 一个 `completed` 的任务被追加消息 → 又跑一遍 → 又产生一个 artifact
- 客户端轮询看到 `completed`，可能**重复消费**产物（发两条通知、记两次账）

**这就是 A2A 里"终态"这个词的分量**：它不是描述性的，而是**行为约束**。

### 6.3 实验 C：同步等结果（不用 `tasks/get`）

把客户端写成"send 之后立刻 `wait` 到死"：功能上能跑，但

- 任务超过 HTTP 超时（通常 30–60 秒）就失败
- 无法取消（你连 `taskId` 都还没拿到就没机会用）
- 无法在等待期间做别的事

**本节的 0ms 返回**不是一个性能优化，而是**能不能支持长任务的开关**。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.a2a_demo
```

实测（节选）：

```
A2A Server（独立进程 pid=19332）: http://127.0.0.1:51929

第 1 步：能力发现 —— GET /.well-known/agent-card.json
  name        : servicedesk-agent
  url         : http://127.0.0.1:51929
  capabilities: {'streaming': True, 'pushNotifications': False, 'stateTransitionHistory': True}
  skills:
    - it_support   IT 支持  tags=['it', 'vpn', 'email']
  按关键词 'vpn' 匹配到的 skill: it_support
  >>> 发现阶段**不产生任何任务**。

第 2 步：派活 —— message/send 立刻返回 task_id
  task_id   : task_88cff7f2ec5a
  state     : submitted
  返回耗时  : 0ms

第 3 步：轮询
  [ 0.01s] submitted       任务已接收
  [ 0.23s] input-required  请提供设备主机名（形如 NB-1024），我才能查资产与权限。
  补上设备号，继续同一个任务（带 taskId，不是新建）：
  [ 0.45s] working         正在检索知识库与设备信息…
  [ 0.67s] completed       已完成
  终态: completed
  产出: 排查建议：1) 在自助门户重置密码；2) 校准手机时间；…
  历史消息数: 3

第 4 步：取消
  新建任务: task_1ff5ba1a4ca4  state=submitted
  取消后  : state=canceled
  再取消一次: A2A 错误：{'code': -32600, 'message': '任务已是终态：canceled'}

第 5 步：任务清单
  task_88cff7f2ec5a    completed       artifacts=1
  task_1ff5ba1a4ca4    canceled        artifacts=0
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 95%]
...............                                                          [100%]
303 passed in 43.06s
```

---

## 8. 生产环境踩坑实录

### 坑 1：按旧文档写了 `tasks/send`

**现象**

对着旧版教程写客户端，调 `tasks/send`，对端返回 `-32601 Method not found`。

**根因**

A2A 从 v1.0 起把方法改成 **`message/send`**（`tasks/send` / `tasks/sendSubscribe` 是被取代的旧名）。
不同实现兼容程度不一。

**修法**

- **新代码一律写 `message/send`**
- 服务端可以像本节一样**同时接受两个名字**，方便过渡期的客户端
- 契约检查：把方法名写进集成测试，升级 SDK 时它会红

**顺手加固**：Agent Card 里 `capabilities` 可以声明支持的交互形态，客户端据此判断——但**方法名本身没有版本协商**，只能靠兼容别名。

### 坑 2：任务表在内存里，服务重启全丢

**现象**

部署更新后，所有 `working` 的任务消失，客户端轮询拿到"任务不存在"。

**根因**

`TaskStore` 是进程内字典（本节就是这样，因为 demo 只需要活几分钟）。

**修法**

换 Redis / 数据库。**这跟 3.4 的 checkpointer 是同一个道理**：
"任务状态"必须活在进程之外，否则重启就是全丢。

**顺手加固**：任务表加 TTL，避免历史任务无限堆积（3.4 坑 3 的同类问题）。

### 坑 3：轮询太密，把对端打挂

**现象**

对端 QPS 暴涨，来源全是同一个客户端。

**根因**

`interval_s=0.05` 这种"反正便宜就多问几次"的写法。

**修法**

- 指数退避：0.1s → 0.2s → 0.4s，封顶 2s
- 尊重 `Retry-After`
- 长任务优先用 `message/stream`（SSE），别轮询

**顺手加固**：客户端记录"已等待时长"，超过阈值改为低频轮询并告警。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 <code>message/send</code> 要立刻返回 task_id，而不是等任务做完？</b></summary>

因为长任务会超过 HTTP 超时，而且等待期间客户端无法取消、无法做别的事。立刻返回 `task_id` 让"派活"和"取结果"解耦，客户端可以轮询、可以取消。实测返回耗时 0ms，而完成要 ~450ms。

</details>

<details>
<summary><b>Q2：补信息时忘了带 <code>taskId</code> 会发生什么？</b></summary>

服务端会**新建一个任务**，原来那个永远停在 `input-required`。实测会留下两个任务：一个僵尸、一个完成。客户端以为在续谈，其实开了新单。

</details>

<details>
<summary><b>Q3：A2A 和 MCP 的边界在哪？</b></summary>

MCP 是 Agent 调用**工具/数据**（短、同步、幂等、`tools/list` 发现）；A2A 是 Agent 之间**派任务**（长、异步、可取消、有状态机、Agent Card 发现）。一句话：MCP 让 Agent 用手，A2A 让 Agent 找人。同一个 Agent 可以两者兼具。

</details>

### 9.2 动手任务：给 A2A 加"流式更新"

**要求**

1. 实现 `message/stream`：用 SSE 把状态变更推给客户端（复用 2.5 的 SSE 写法）。
2. 客户端加 `stream()` 方法：边收边打印，不再轮询。
3. 对比：轮询 vs 流式在"任务 3 秒完成"下的请求次数与首条延迟。
4. 补 3 个用例：正常流完、客户端中途断开、任务失败时的流式错误。

**验收标准**

- 流式模式下**请求次数为 1**（轮询是 N 次）。
- 首条状态更新的延迟 < 100ms。
- 断开后服务端任务**继续执行**（A2A 的任务生命周期与连接无关）。
- 原有 303 个用例仍全绿。

**提示**：第 3 条容易踩错——很多人以为"连接断了任务就该停"，其实**取消要显式调 `tasks/cancel`**。

### 9.3 拓展挑战

实现"多 Agent 接力"：A 发现 B，把活派给 B；B 又发现 C，把活的子任务派给 C。

**思考**：

1. `contextId` 该怎么沿链路传递？（提示：它是"这段对话"的标识）
2. 如果 C 失败，B 该怎么向 A 汇报？（提示：任务状态 `failed` 与 `error` 字段）
3. 链路变长后，超时该在哪一层设？每层都设会不会导致"外层先超时"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **Agent Card 是发现的入口**：`GET /.well-known/agent-card.json`，看 `skills` 决定派不派活，**发现不产生任务**。
2. **`message/send` 先接单后干活**（实测 0ms 返回），长任务靠 `tasks/get` 轮询；`input-required` 补信息**必须带 `taskId`**，否则会新建任务（实测留下僵尸）。
3. **终态是行为约束不是描述**：`completed`/`canceled`/`failed`/`rejected` 之后不能再续，否则会重复交付。

**已知限制（诚实记录）**

- **未实现 `message/stream`**：只有轮询（§9.2 是作业）。
- **任务表在内存**：进程重启全丢（生产必须换 Redis/DB）。
- **无鉴权**：任何知道地址的人都能派活、能 `tasks/list`（生产必须加，2.6 的 scope 经验可用）。
- **`configuration`/`artifact` 字段是子集**（⚠️ 见文首）。
- **无 `contextId` 生命周期管理**：不做过期、不清理。

**延伸阅读**

- A2A 规范（Linux Foundation）：https://a2a-protocol.org/latest/specification/
- Agent Card 与 `message/send`：[A2A v1.0 对齐清单](https://github.com/ariffazil/aaa/blob/fcc2d2f0abe2f3e400ccd63d5a5269a9eec66226/a2a-server/ALIGNMENT_MD.md)
- 方法名演进：[Latest A2A Protocol Features](https://github.com/vishalmysore/a2ajava/blob/107dda885b204c8c16e9ae39fd4e9c68b4a6c904/tutorial/12_A2A_Latest_Protocol_Features.md)

---

## 修订记录

- 2026-11-18 初稿：六步演示与 14 个测试全部实机运行；子进程 Server + 轮询 + 取消 + 终态保护均验证
- 2026-11-18 更正大纲：方法名从 `tasks/send` 改为 **`message/send`**（v1.0），并保留旧名为兼容别名
- 2026-11-18 修正：`A2AClient.wait()` 初版会在"补信息后第一次轮询仍是旧状态"时立刻返回；改为可配 `stop_on_input_required` 与 `known_state`
- 2026-11-18 修正：服务端"是否提到设备"的判断原先内联在 `_run_task` 里且逻辑绕，抽成 `_mentions_device` / `_history_texts`
- 2026-11-18 实测补充：忘记带 `taskId` 会新建任务并留下僵尸（原任务停在 `input-required`）
