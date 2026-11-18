# file: src/servicedesk/graph/checkpoint.py
"""Checkpointer：让图能"从上次成功的节点继续"。

三件事必须同时成立，断点续跑才真的可用：

1. **thread_id**：同一会话的所有步骤落在同一个 thread 下（否则恢复时找不到）
2. **持久化存储**：内存版只活在进程里；崩了要换 SQLite/Postgres
3. **幂等**：被中断的那个节点会**重新执行**，副作用不能重复发生

本节还会演示"时间旅行"（回看历史状态）与"schema 变了怎么兼容"。
"""

from __future__ import annotations

import asyncio
import operator
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, TypedDict

from langgraph.graph import END, START, StateGraph

# ------------------------------- State -------------------------------


class WorkState(TypedDict, total=False):
    ticket_id: str
    step: int                       # 已完成到第几步
    trace: Annotated[list[str], operator.add]
    answer: str
    error: str | None


TOTAL_STEPS = 4
CRASH_ENV = "CKPT_CRASH_AT"          # 设成 3 就表示「在第 3 步进程被杀」
SIDE_EFFECT_ENV = "CKPT_SIDE_EFFECT_FILE"   # 每步往这个文件追加一行，用来数"重跑了几次"


# ------------------------------- 节点 -------------------------------


def make_step(index: int):
    """造一个"干一会儿活"的节点。

    `os._exit(1)` 是刻意选的：它**不走 finally、不做清理**，和 `kill -9` 最像。
    用 `sys.exit()` 或抛异常都太温柔，测不出"进程突然没了"的真实情况。
    """

    async def node(state: WorkState) -> dict[str, Any]:
        await asyncio.sleep(0.05)

        # 先产生一个"不可撤销的副作用"（发通知、扣款、写外部系统）
        side_effect_file = os.environ.get(SIDE_EFFECT_ENV)
        if side_effect_file:
            with open(side_effect_file, "a", encoding="utf-8") as handle:
                handle.write(f"step{index}\n")

        if str(os.environ.get(CRASH_ENV, "")) == str(index):
            os._exit(1)          # 崩在"副作用已发生、checkpoint 还没写"的窗口里

        return {"step": index, "trace": [f"step{index}"]}

    return node


def finish(state: WorkState) -> dict[str, Any]:
    return {"answer": f"工单 {state.get('ticket_id', '?')} 处理完成（共 {state.get('step')} 步）"}


def build_workflow_graph(checkpointer: Any = None):
    """4 个串行步骤 + 一个收尾节点。挂不挂 checkpointer 都能跑。"""
    builder = StateGraph(WorkState)
    for index in range(1, TOTAL_STEPS + 1):
        builder.add_node(f"step{index}", make_step(index))
    builder.add_node("finish", finish)

    builder.add_edge(START, "step1")
    for index in range(1, TOTAL_STEPS):
        builder.add_edge(f"step{index}", f"step{index + 1}")
    builder.add_edge(f"step{TOTAL_STEPS}", "finish")
    builder.add_edge("finish", END)

    return builder.compile(checkpointer=checkpointer)


# ------------------------------- 三种存储 -------------------------------


@asynccontextmanager
async def open_sqlite_checkpointer(path: str | Path) -> AsyncIterator[Any]:
    """SQLite：最简单可落盘的 Checkpointer，适合单机与小规模。

    **必须用 AsyncSqliteSaver**：同步的 `SqliteSaver` 不支持 `ainvoke`，
    会抛 `NotImplementedError: The SqliteSaver does not support async methods`。
    图是异步的，saver 就必须是异步的。
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()          # 建表；第一次运行必须有
        yield saver


def make_memory_checkpointer() -> Any:
    """内存：进程一停就没了。只适合测试与 demo。"""
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def sqlite_checkpointer_factory(path: str | Path):
    """把「打开方式」包成工厂，方便和 Postgres 版互换。"""
    return lambda: open_sqlite_checkpointer(path)


# Postgres 版（本节未运行：需要真实数据库）
# from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
# async with AsyncPostgresSaver.from_conn_string("postgresql://user:pass@host:5432/db") as saver:
#     await saver.setup()
#     ...  # 与 AsyncSqliteSaver 用法完全一致，只是连接串不同


# ------------------------------- 恢复与观测 -------------------------------


def thread_config(thread_id: str) -> dict[str, Any]:
    """同一个 thread_id = 同一个会话。换 id 就是另一段独立历史。"""
    return {"configurable": {"thread_id": thread_id}}


async def run(graph: Any, thread_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """传 payload = 从头跑；传 None = 从上次的断点继续。"""
    return await graph.ainvoke(payload, config=thread_config(thread_id))


async def snapshot(graph: Any, thread_id: str) -> dict[str, Any]:
    """看一眼当前停在哪：`next` 是"下一步要执行的节点"。

    用 `aget_state`：异步 saver 不支持同步的 `get_state`。
    """
    state = await graph.aget_state(thread_config(thread_id))
    return {
        "next": tuple(state.next),
        "step": state.values.get("step", 0),
        "trace": list(state.values.get("trace", [])),
        "checkpoint_id": (state.config.get("configurable") or {}).get("checkpoint_id"),
        "answer": state.values.get("answer"),
    }


async def history(graph: Any, thread_id: str) -> list[dict[str, Any]]:
    """时间旅行：把这个 thread 的所有历史快照列出来（新的在前）。"""
    rows: list[dict[str, Any]] = []
    async for state in graph.aget_state_history(thread_config(thread_id)):
        configurable = state.config.get("configurable") or {}
        rows.append(
            {
                "checkpoint_id": configurable.get("checkpoint_id"),
                "next": tuple(state.next),
                "step": state.values.get("step", 0),
                "trace": list(state.values.get("trace", [])),
            }
        )
    return rows


async def replay(graph: Any, thread_id: str, checkpoint_id: str) -> dict[str, Any]:
    """从某个历史快照重新往下跑（用于"回滚到某一步重试"）。"""
    config = {
        "configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}
    }
    return await graph.ainvoke(None, config=config)


def ensure_defaults(state: dict[str, Any]) -> dict[str, Any]:
    """Schema 演进：老 checkpoint 里没有的字段，补默认值。

    因为 State 声明为 `total=False`，缺字段不会导致反序列化失败——
    但**下游代码可能假设它存在**，所以恢复时统一补一遍。
    """
    defaults: dict[str, Any] = {"step": 0, "trace": [], "answer": "", "error": None}
    return {**defaults, **state}
