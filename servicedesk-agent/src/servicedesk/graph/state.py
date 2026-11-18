# file: src/servicedesk/graph/state.py
"""State 设计：什么该进 State，什么不该。

两条规则，本节全部围绕它们：

    规则 1：**并发/多次写入的字段，必须声明 reducer**，否则后来的覆盖先前的。
    规则 2：**能由别的字段算出来的，就不要存**——多存一份就多一个不一致的机会。

State 用 TypedDict（图需要它做静态键检查），值里可以嵌 pydantic 模型做运行时校验。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

# ------------------------------- 自定义 reducer -------------------------------


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """合并两个字典，右边的同名键覆盖左边。

    和 list 的 `operator.add` 不同：字典没有"追加"语义，
    默认行为（整体覆盖）又会把之前收集的来源全丢掉，所以必须自己写。

    注意两个参数都可能是 None —— LangGraph 在字段首次写入时传 None。
    """
    return {**(left or {}), **(right or {})}


# ------------------------------- State -------------------------------


class TicketState(TypedDict, total=False):
    """工单处理图的状态。

    字段分三类，注释里标了各自该用什么 reducer：
        [累加]  messages / trace / sources  —— 多次写入要累积
        [覆盖]  intent / draft / ticket_id  —— 只保留最新值
        [不存]  needs_approval              —— 由 intent + draft 算出来，见下方
    """

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

    # ---- 刻意不存 ----
    # needs_approval 由 intent + draft 决定。存下来会有两个后果：
    #   ① 改了 intent 忘了同步它 → 状态自相矛盾
    #   ② checkpointer 里多一份可推导的数据 → 恢复时更难判断谁是对的
    # 所以它写成函数（derive_needs_approval），需要时现算。


def derive_needs_approval(intent: str | None, draft: str | None) -> bool:
    """由 State 里已有的字段推导"要不要审批"。

    这是「可重算的中间量」的标准处理方式：**不存，现算**。
    """
    return intent == "vpn" and "重置" in (draft or "")


# ------------------------------- 可选：pydantic 校验值 -------------------------------


class CopyHint(TypedDict, total=False):
    """示例：State 里嵌 pydantic 模型也可以（做运行时校验）。

    这里用 TypedDict 保持零依赖；真实项目里把 summary 换成
    `TicketSummary`（pydantic BaseModel）就能获得字段级校验。
    """

    summary: dict[str, Any]


# ------------------------------- 使用示例 -------------------------------


def describe_state_schema() -> dict[str, str]:
    """把每个字段的"写入语义"列出来，方便写进设计文档。

    返回值：字段名 → 写入语义（累加 / 覆盖 / 不存）。
    """
    annotations = TicketState.__annotations__
    result: dict[str, str] = {}
    for name in annotations:
        if name in ("messages", "trace", "sources"):
            result[name] = "累加（有 reducer）"
        else:
            result[name] = "覆盖（后来者胜）"
    result["needs_approval"] = "不存（derive_needs_approval 现算）"
    return result
