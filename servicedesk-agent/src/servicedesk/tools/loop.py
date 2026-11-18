# file: src/servicedesk/tools/loop.py
"""工具调用循环：模型要工具 → 我们执行 → 结果回喂 → 直到它给出最终回答。

这个循环就是「Agent」的最小形态。三个必须做对的地方：
1. **assistant 的 tool_calls 必须原样回填**，再跟 tool 消息（顺序铁律）
2. **每个 tool 消息必须带 tool_call_id**，否则模型对不上号
3. **必须有轮次上限**，否则模型可能无限调工具
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from servicedesk.errors import FatalError
from servicedesk.llm.client import LLMClient
from servicedesk.llm.raw_client import Message
from servicedesk.logging_setup import get_logger
from servicedesk.tools.registry import ToolRegistry, ToolResult

log = get_logger("tools.loop")


@dataclass
class ToolCallRecord:
    """一次工具调用的完整记录，用于审计与回放。"""

    name: str
    arguments: str
    result: ToolResult
    call_id: str = ""


@dataclass
class ToolLoopResult:
    answer: str
    rounds: int
    calls: list[ToolCallRecord] = field(default_factory=list)
    cost_cny: float = 0.0

    @property
    def called_tools(self) -> list[str]:
        return [record.name for record in self.calls]


async def run_tool_loop(
    client: LLMClient,
    registry: ToolRegistry,
    question: str,
    *,
    max_rounds: int = 4,
    model: str | None = None,
    system: str | None = None,
) -> ToolLoopResult:
    """跑完一次「问 → 调工具 → 答」的完整往返。"""
    messages: list[Message] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": question})

    calls: list[ToolCallRecord] = []
    cost = 0.0

    for round_index in range(1, max_rounds + 1):
        result = await client.chat(
            messages,
            model=model,
            tools=registry.schemas(),
        )
        cost += result.cost_cny

        if not result.tool_calls:
            log.info(
                "tool_loop_done",
                rounds=round_index,
                tool_calls=len(calls),
                cost_cny=round(cost, 6),
            )
            return ToolLoopResult(
                answer=result.text,
                rounds=round_index,
                calls=calls,
                cost_cny=cost,
            )

        # 1) 先把 assistant「要调工具」这条消息原样放回去
        messages.append(
            {
                "role": "assistant",
                "content": result.text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in result.tool_calls
                ],
            }
        )

        # 2) 逐个执行，并把结果以 tool 角色追加（必须带 tool_call_id）
        for call in result.tool_calls:
            outcome = await registry.call(call.name, call.arguments)
            calls.append(
                ToolCallRecord(
                    name=call.name,
                    arguments=call.arguments,
                    result=outcome,
                    call_id=call.id,
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": outcome.content,
                }
            )

        log.info(
            "tool_round_done",
            round=round_index,
            tools=[call.name for call in result.tool_calls],
        )

    raise FatalError(
        f"工具调用超过 {max_rounds} 轮仍未给出最终回答，已中止（可能是工具总是失败或模型陷入循环）"
    )


def summarize(result: ToolLoopResult) -> dict[str, Any]:
    """给日志/前端用的摘要。"""
    return {
        "rounds": result.rounds,
        "tools": result.called_tools,
        "failed": [record.name for record in result.calls if not record.result.ok],
        "cost_cny": round(result.cost_cny, 6),
        "answer_preview": result.answer[:120],
    }
