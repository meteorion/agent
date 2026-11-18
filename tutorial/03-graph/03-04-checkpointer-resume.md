---
section: "3.4"
title: "用 Checkpointer 实现「杀进程后断点续跑」"
stage: 3
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M3
depends_on: ["3.3"]
produces: ["graph/checkpoint.py"]
verifies: "杀进程重启能从 3.3 中途节点继续且结果一致"
part: null
updated: "2026-11-18"
---

# 3.4 用 Checkpointer 实现「杀进程后断点续跑」

## 0. 本节导航

**本节在链路中的位置**：3.1 实测过"崩一次，6 次执行做 3 步活"。本节给出正式解法。这是 M3 里最"工程"的一节——**不是讲概念，是真的把进程杀掉再拉起来**。

**本节你将获得**

1. `thread_id` / checkpoint / `next` 三元组的作用。
2. 内存 → SQLite 的切换，以及**为什么必须用 Async 版**（实测报错）。
3. **真杀进程**实验：`os._exit(1)` 之后重启，从第 2 步继续，`trace` 证明没有从头重来。
4. 一个刺眼但必须知道的结论：**`step3` 的副作用执行了 2 次**。
5. 时间旅行：`get_state_history` 回看每一步、从旧快照重放。
6. Schema 演进时老 checkpoint 的兼容处理。

**前置检查清单**

- 3.3 的 `uv run python -m scripts.edges_demo` 能跑通。
- 知道 `os._exit` 和 `sys.exit` 的区别（本节会用到）。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

3.1 的实测摆在这里：

```
第一次运行：在第 3 步崩溃（下游服务超时）
  已执行步骤数: 3
重跑：ok，又执行了 3 步
  → 为了完成 3 个步骤的工作，总共执行了 6 次
```

放到生产里，这一句话对应三笔账：

- **钱**：三次下游调用（LLM、检索、推送）全部重复计费
- **时间**：用户多等一倍
- **副作用**：如果第 2 步已经发了通知，重跑会让用户收到两条

而"断点续跑"听起来简单，实现起来要同时满足三件事：

1. **知道这段任务是谁**（`thread_id`）
2. **状态活着**（不能只在内存里）
3. **知道下一步从哪开始**（`next`）

少任何一样，恢复就只能"从头来"。

---

## 2. 核心概念与架构设计

### 2.1 类比：游戏存档

- **`thread_id` = 存档槽位**：同一槽位共享一段历史，换槽位就是另开一局
- **checkpoint = 每过一关自动存档**：不是手动存，是**每个节点执行完自动存**
- **`next` = 下次读档后该打的关**：告诉你"从这里继续"

> **这个类比在哪失效**：游戏读档不会让"已打败的 boss 复活"。而图恢复时，
> **被中断的那个节点会重新执行一次**——如果它有副作用，副作用就发生了两次。
> §6.3 会实测给你看。

### 2.2 三元组

```python
config = {"configurable": {"thread_id": "t-1001"}}
state  = await graph.aget_state(config)
# state.next         → ('step3',)          下一步执行谁
# state.values       → {...}               当前状态
# state.config["configurable"]["checkpoint_id"] → '1f1e30b5-…'
```

**恢复就是再 `ainvoke` 一次，输入传 `None`**：

```python
await graph.ainvoke(None, config=config)     # 从 next 继续
```

### 2.3 存档时机

**每执行完一个节点，存一次。** 不是"整图跑完才存"。

所以一个 4 步的图，正常跑完会产生 **5 个以上**快照（起始 1 个 + 每步 1 个 + 收尾）。
实测本节的 4 步图有 **7 个**快照（含 `finish` 前后的两次）。

### 2.4 三种存储

| 类型 | 优点 | 代价 | 适用 |
|---|---|---|---|
| `InMemorySaver` | 零配置、最快 | 进程一停就没了 | 测试 / demo |
| `AsyncSqliteSaver` | 单文件落盘、零服务依赖 | 并发写有锁竞争 | 单机 / 小规模 |
| `AsyncPostgresSaver` | 并发安全、多实例共享 | 要运维数据库 | 生产 / 多实例 |

**调用方式完全一样，只换构造。** 本节实测了前两种；Postgres 版需要真实数据库，未运行（代码见 `checkpoint.py` 注释）。

### 2.5 时间旅行

```python
async for state in graph.aget_state_history(config):
    ...    # 新的在前
```

每个快照都带 `checkpoint_id`，可以**从任意一个快照重放**：

```python
await graph.ainvoke(None, config={**config, "configurable": {**config["configurable"],
                                                            "checkpoint_id": old_id}})
```

**这会从那个快照之后重新往下跑，并在同一个 thread 上形成新分支。**
适合"第 3 步的结果不理想，回到第 2 步换个参数重来"。

### 2.6 幂等：本节最重要的一句话

> **Checkpointer 保证"不重复劳动"，不保证"副作用不重复"。**

被中断的节点会**重新执行**。所以节点里的外部写操作必须是幂等的：

- 发通知 → 用 `message_id` 去重
- 扣款 → 用业务单号做幂等键
- 建工单 → 用内容哈希做幂等键（2.6 的 `create_ticket` 就是这么做的）

§6.3 的实测：`step3` 的副作用执行了 **2 次**，而 `trace` 里 `step3` 只出现 **1 次**——
因为崩溃那次没走到 `return`，所以没写 trace，但副作用已经发生了。

### 2.7 适用范围（边界）

| 场景 | 需要 checkpointer 吗 |
|---|---|
| 纯读、无副作用的图 | ❌ 不需要（重跑就重跑） |
| 有外部写操作 | ✅ 必须，且要幂等 |
| 长任务（> 30 秒） | ✅ 必须，否则一次抖动全废 |
| 需要人工审批（3.5） | ✅ **必须**，否则挂起后无处恢复 |
| 需要回滚 / 重试某一步 | ✅ 时间旅行 |

---

## 3. 环境与版本

```powershell
uv add langgraph-checkpoint-sqlite
```

| 组件 | 实测版本 |
|---|---|
| `langgraph-checkpoint-sqlite` | **3.1.3** |
| `aiosqlite` | `0.22.1` |
| `langgraph` | `1.2.22` |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver; print('ok')"
```

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/graph/checkpoint.py` | **主角**：图 + 三种存储 + 恢复/历史/重放 |
| `scripts/checkpoint_worker.py` | 子进程：跑到第 3 步 `os._exit(1)` |
| `scripts/checkpoint_demo.py` | 六步演示（含真杀进程） |
| `tests/test_graph_checkpoint.py` | 10 个断言（含一个真杀进程的集成测试） |

### 4.1 让节点"能被真正杀死"

```python
def make_step(index: int):
    """`os._exit(1)` 是刻意选的：它**不走 finally、不做清理**，和 `kill -9` 最像。"""
    async def node(state: WorkState) -> dict[str, Any]:
        await asyncio.sleep(0.05)

        # 先产生一个"不可撤销的副作用"
        side_effect_file = os.environ.get(SIDE_EFFECT_ENV)
        if side_effect_file:
            with open(side_effect_file, "a", encoding="utf-8") as handle:
                handle.write(f"step{index}\n")

        if str(os.environ.get(CRASH_ENV, "")) == str(index):
            os._exit(1)          # 崩在"副作用已发生、checkpoint 还没写"的窗口里

        return {"step": index, "trace": [f"step{index}"]}
    return node
```

### 4.2 SQLite：必须是 Async 版

```python
@asynccontextmanager
async def open_sqlite_checkpointer(path: str | Path) -> AsyncIterator[Any]:
    """**必须用 AsyncSqliteSaver**：同步的 `SqliteSaver` 不支持 `ainvoke`。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()          # 建表；第一次运行必须有
        yield saver
```

### 4.3 读出断点

```python
async def snapshot(graph: Any, thread_id: str) -> dict[str, Any]:
    """用 `aget_state`：异步 saver 不支持同步的 `get_state`。"""
    state = await graph.aget_state(thread_config(thread_id))
    return {
        "next": tuple(state.next),
        "step": state.values.get("step", 0),
        "trace": list(state.values.get("trace", [])),
        "checkpoint_id": (state.config.get("configurable") or {}).get("checkpoint_id"),
    }
```

### 4.4 子进程被杀

```python
async def main() -> int:
    db_path, thread_id = sys.argv[1], sys.argv[2]
    async with open_sqlite_checkpointer(db_path) as saver:
        graph = build_workflow_graph(saver)
        out = await run(graph, thread_id, {"ticket_id": "T-1001", "trace": []})
        print(f"子进程正常完成: step={out.get('step')}")
    return 0
```

父进程用 `subprocess.run(..., env={**os.environ, "CKPT_CRASH_AT": "3"})` 拉起它。

### 4.5 运行

```powershell
uv run python -m scripts.checkpoint_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么必须 Async 版

我第一版用的是同步 `SqliteSaver`：

```python
from langgraph.checkpoint.sqlite import SqliteSaver
with SqliteSaver.from_conn_string(path) as saver:
    ...
```

实测直接报：

```
NotImplementedError: The SqliteSaver does not support async methods.
Consider using AsyncSqliteSaver instead.
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
```

**原因很直白**：图是 `ainvoke`（异步），saver 是同步实现。
**异步图必须配异步 saver。**

### 5.2 `get_state` 也一样不能用

改完 saver 之后，读状态又炸了一次：

```
async with AsyncSqliteSaver... -> graph.get_state(...)
NotImplementedError
```

因为它内部还是走同步接口。**所有读操作都要换成异步版**：

| 同步 | 异步 |
|---|---|
| `get_state` | `aget_state` |
| `get_state_history` | `aget_state_history` |
| `invoke` | `ainvoke` |

**规律**：只要 saver 用了异步实现，`graph` 的所有方法都用 `a` 前缀那个。

### 5.3 `os._exit` vs 异常：为什么不用异常模拟

用异常模拟崩溃太"温柔"：

- 异常会走 `finally` → 连接被正常关闭 → 数据被刷盘
- 异常后进程还活着 → 内存里的东西还在

**`os._exit(1)` 才是真的"进程突然没了"**：不执行 `finally`、不刷缓冲、不关连接。
本节用它来测，才测出了真实行为。

### 5.4 `next` 为什么是元组

因为一个节点可以**并行**推进到多个后继（`next=('a', 'b')`）。
本节是串行图，所以永远是单元素元组。**别用 `next[0]` 的写法**——并行图会踩坑。

### 5.5 `await saver.setup()` 不能忘

`AsyncSqliteSaver` 不会自动建表。第一次运行不调 `setup()`，会在写 checkpoint 时报"no such table"。

**生产里应该在应用启动时调一次**（幂等），而不是每次请求都调。

### 5.6 恢复时传 `None` 的含义

```python
await graph.ainvoke(None, config=config)
```

`None` = "没有新的输入，接着上次的 `next` 往下跑"。

如果你传一个 dict，LangGraph 会把它当成**新一轮**的输入（覆盖 State 里的键），
而不是"继续"——**这是最容易搞混的一点**。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：复用 `thread_id` 导致历史串台

如果用同一个 `thread_id` 处理两张不同的工单，第二张会**继承第一张的 State**
（因为它在同一个存档槽里）。

**症状**：第二张工单的 `trace` 里带着第一张的步骤，`step` 从 4 开始。

**修法**：`thread_id` 必须唯一到"一次任务"，通常是 `订单号` / `工单号` + 尝试序号。

### 6.2 实验 B：同步 saver + 异步图

见 §5.1，实测 `NotImplementedError`。

### 6.3 实验 C：**被中断的节点会重跑，副作用会重复**

这是本节最重要的实测。`make_step` 在 `os._exit` **之前**先往文件里追加一行，
然后在第 3 步崩掉；恢复后统计每步出现了几次：

```
副作用文件（每步追加一行）: {'step1': 1, 'step2': 1, 'step3': 2, 'step4': 1}
>>> step3 出现了 2 次！
```

而 `trace` 是：

```
trace=['step1', 'step2', 'step3', 'step4']      ← step3 只出现 1 次
```

**为什么两个数字不一样？**

- 崩溃那次：副作用**已经发生**（文件写了一行），但没走到 `return`，所以 **trace 没写**
- 恢复那次：重新执行 `step3`，副作用**又发生一次**，这次走到了 `return`，trace 写了

**结论**：`trace` 看起来"很干净"，但真实世界里的副作用已经执行了两次。

> 这就是为什么"发通知、扣款、建单"必须幂等。**Checkpointer 保的是状态，不是副作用。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.checkpoint_demo
```

实测（节选）：

```
总步数: 4
thread_config 示例: {'configurable': {'thread_id': 'demo-thread'}}

第 1 步：内存 Checkpointer —— 同进程内恢复（异常版）
  第一次运行失败: 第 3 步调用的下游接口 500
  失败后停在哪: step=2 trace=['step1', 'step2'] next=('step3',)
  >>> 注意 next：下一步是 step3 —— 说明前两步的成果被保住了。
  恢复后跑完: step=4 trace=['step1', 'step2', 'step3', 'step4']

第 2 步：真的杀进程 —— os._exit(1)，无清理、无 finally
  子进程环境: CKPT_CRASH_AT=3（跑到第 3 步直接 os._exit）
  第一次退出码: 1（非 0 表示被中断）
  重启后读到的断点: step=2 trace=['step1', 'step2'] next=('step3',)
  续跑完成: step=4 trace=['step1', 'step2', 'step3', 'step4']
  答复: 工单 T-1001 处理完成（共 4 步）

  >>> 关键点：trace 是 step1,step2,step3,step4 —— 没有从头重来。

  副作用文件（每步追加一行）: {'step1': 1, 'step2': 1, 'step3': 2, 'step4': 1}
  >>> step3 出现了 2 次！

第 3 步：时间旅行 —— 回看每一步的快照
  共 7 个快照（新的在前）：
    step=4  next=() trace=['step1', 'step2', 'step3', 'step4']
    step=4  next=('finish',) trace=[...]
    step=3  next=('step4',) trace=['step1', 'step2', 'step3']
    step=2  next=('step3',) trace=['step1', 'step2']
    step=1  next=('step2',) trace=['step1']
    step=0  next=('step1',) trace=[]

第 4 步：从历史快照重放
  选中 checkpoint=1f1e30b5-be84-6846-8… （step=2）
  重放结果: step=4 trace=['step1', 'step2', 'step3', 'step4']

第 5 步：Schema 演进
  老快照: {'ticket_id': 'T-3001', 'trace': ['step1']}
  补默认后: {'step': 0, 'trace': ['step1'], 'answer': '', 'error': None, 'ticket_id': 'T-3001'}
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 88%]
.............................                                            [100%]
245 passed in 28.34s
```

其中 `test_killed_process_resumes_from_last_completed_node` 是**真的起子进程、真的 `os._exit`**，
并在新进程里读同一个 SQLite 文件。

---

## 8. 生产环境踩坑实录

### 坑 1：用同步 saver 配异步图

**现象**

```
NotImplementedError: The SqliteSaver does not support async methods.
```

**定位方法**

看 saver 的类型：`type(saver).__name__`。`SqliteSaver` 是同步版，`AsyncSqliteSaver` 才是异步版。

**修法**

```diff
- from langgraph.checkpoint.sqlite import SqliteSaver
- with SqliteSaver.from_conn_string(path) as saver:
+ from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
+ async with AsyncSqliteSaver.from_conn_string(path) as saver:
+     await saver.setup()
```

**顺手加固**：把 `get_state` / `get_state_history` 一并换成 `aget_*`，否则下一步还会炸。

### 坑 2：被中断的节点重跑，通知发了两次

**现象**

用户投诉"同一条工单我收到了两条短信"。

**定位方法**

在副作用处打计数（本节 `CKPT_SIDE_EFFECT_FILE` 的做法）：

```
{'step1': 1, 'step2': 1, 'step3': 2, 'step4': 1}
```

`step3` 是 2 → 它在"副作用已发生、checkpoint 未写"的窗口里崩过。

**根因**

checkpointer 只保证"从最后一个**成功完成**的节点继续"。被中断的那个节点**从头执行**。

**修法**

给副作用加幂等键：

```python
key = f"{thread_id}:{node}:{step}"
if await already_done(key):
    return {"step": step}          # 跳过，不再发
await send_notification(...)
await mark_done(key)
```

**顺手加固**：把"发通知"这类操作拆成**独立节点**放在链尾（只执行一次），
而不是埋在中间步骤里。

### 坑 3：checkpoint 表无限增长

**现象**

SQLite 文件涨到几个 GB，`get_state_history` 越来越慢。

**根因**

每个节点都存一次，永不清理。

**修法**

- 定期按 `thread_id` + 时间清理旧 thread（保留最近 N 天）
- 或只保留"每步最新快照"，删除中间态（需要自定义 saver）
- Postgres 版用分区表 + 定期 drop 分区

**顺手加固**：监控 checkpoint 表行数与磁盘占用，设告警阈值。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么恢复时 `ainvoke` 要传 <code>None</code>？</b></summary>

`None` 表示"没有新输入，接着上次的 `next` 往下跑"。传一个 dict 会被当成新一轮的输入去覆盖 State 的键，而不是续跑——这是最容易搞混的点。

</details>

<details>
<summary><b>Q2：`trace` 里 `step3` 只出现一次，为什么副作用文件里是两次？</b></summary>

因为崩溃发生在"副作用已执行、`return` 还没执行"的窗口。那次执行写了文件，但没写进 State；恢复时又把 `step3` 整个重跑了一遍，所以文件里第二行。**Checkpointer 保证的是状态一致，不是副作用唯一。**

</details>

<details>
<summary><b>Q3：为什么用 <code>os._exit</code> 而不是抛异常来模拟崩溃？</b></summary>

抛异常会走 `finally`、会正常关闭连接、会刷盘，进程也还活着——测不出真实情况。`os._exit` 跳过所有清理，等价于 `kill -9`，才能验证"checkpoint 到底有没有落盘"。

</details>

### 9.2 动手任务：给 3.3 的图挂上 checkpointer

**要求**

1. 给 `build_branching_graph` 加 `checkpointer` 参数，`thread_id` 用工单号。
2. 在 `fetch_vpn` 里模拟"第 2 次调用时进程被杀"（用 `os._exit`），验证恢复后**不会重新走 `classify`**。
3. 给 `search_knowledge` 这类"只读"操作加缓存节点，验证恢复时命中缓存而不是重新检索。
4. 补 3 个用例：断点在中途、断点在末尾、不同 thread 隔离。

**验收标准**

- 恢复后 `trace` 从断点继续，而不是从头。
- 只读操作在恢复时不重复执行（可用计数器验证）。
- 原有 245 个用例仍全绿。

**提示**：第 2 条要跑在子进程里才能真的杀掉，参考 `scripts/checkpoint_worker.py`。

### 9.3 拓展挑战

实现一个"checkpoint 清理器"：保留最近 N 个 thread、每个 thread 只保留最近 M 个快照。

**思考**：

1. 删除中间快照会影响时间旅行吗？影响哪些操作？
2. 如果一个 thread 还在运行（`next` 非空），能删它的最新快照吗？
3. Postgres 版该怎么高效删除（提示：`thread_id` + `checkpoint_id` 的索引设计）？

---

## 10. 本节小结与延伸

**三点回顾**

1. **三元组**：`thread_id`（哪段）+ checkpoint（状态）+ `next`（从哪继续）。恢复 = `ainvoke(None, config)`。
2. **必须全异步**：异步图配 `AsyncSqliteSaver`，读状态用 `aget_state` / `aget_state_history`（实测同步版直接 `NotImplementedError`）。
3. **Checkpointer 保状态，不保副作用**：实测被中断的 `step3` 副作用执行了 **2 次**，而 `trace` 里只出现 1 次——**别被干净的 trace 骗了**。

**已知限制（诚实记录）**

- **Postgres 版未运行**：需要真实数据库，只有代码路径，没有实测。
- **没做 checkpoint 清理**：SQLite 文件会随运行无限增长（§9.3 是作业）。
- **节点没做幂等**：本节的 `make_step` 故意不幂等，用来暴露"重跑"的后果；生产必须加幂等键。
- **只测了串行图**：并行节点的 `next` 是多元素元组，恢复语义未验证。

**延伸阅读**

- LangGraph 持久化与 Checkpointer：https://langchain-ai.github.io/langgraph/concepts/persistence/
- 时间旅行：https://langchain-ai.github.io/langgraph/concepts/time-travel/
- `AsyncSqliteSaver` API：https://langchain-ai.github.io/langgraph/reference/checkpoints/#langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver

---

## 修订记录

- 2026-11-18 初稿：六步演示与 10 个测试全部实机运行；真杀进程实验（子进程 `os._exit`）通过
- 2026-11-18 修正：初版用同步 `SqliteSaver` 导致 `NotImplementedError`，改为 `AsyncSqliteSaver` 并补 `await saver.setup()`
- 2026-11-18 修正：`get_state` / `get_state_history` 在异步 saver 下同样不可用，改为 `aget_state` / `aget_state_history`
- 2026-11-18 补充：给节点加"副作用计数器"，实测中断节点重跑导致 `step3` 副作用执行 2 次（初版只看 `trace`，看不出重跑）
