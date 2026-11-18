# file: src/servicedesk/memory/short_term.py
"""短期会话记忆：按 token 预算裁剪，且**不能丢掉约束**。

三种策略，差别只在"丢什么、留什么"：

    window   —— 丢最旧的，留最近的
    summary  —— 把丢掉的压成一句话
    hybrid   —— 约束固定保留 + 中段摘要 + 最近若干条原样

本节要证明的一件事：**前两种都可能丢掉关键约束**（工单号、"不要重置密码"），
而 hybrid 靠"约束抽取 + 固定保留"把它们钉住。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Strategy = Literal["window", "summary", "hybrid"]

# ------------------------------- token 估算 -------------------------------

# 粗估：CJK 约 1 token/字，ASCII 约 1 token/4 字符。
# 没有 tokenizer 时的务实做法——用于**比值与预算**，不是精确计费。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, round(cjk + other / 4))


# ------------------------------- 消息模型 -------------------------------


@dataclass
class Message:
    role: str                     # user / assistant / tool / system
    content: str
    pinned: bool = False          # 是否永久保留（约束类）

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


# ------------------------------ 约束抽取 ------------------------------

_TICKET_RE = re.compile(r"T-\d{4,}")
_APPROVAL_WORDS = ("已批准", "已通过", "审批通过", "已审批")

# 抽"短语"而不是整个消息：用负向前瞻排除「要不要」（那是疑问，不是禁止）
_CLAUSE = r"[^，。！？；\n]{0,24}"
_PROHIBIT_RE = re.compile(rf"(?<!要)不要{_CLAUSE}|禁止{_CLAUSE}|不得{_CLAUSE}")
_MUST_RE = re.compile(rf"必须{_CLAUSE}|一定要{_CLAUSE}|务必{_CLAUSE}")


@dataclass(frozen=True)
class Constraint:
    kind: str            # ticket / approval / prohibit / must
    text: str


def extract_constraints(messages: list[Message]) -> list[Constraint]:
    """从消息里抽出"丢了会出事"的信息。

    真实系统里这一步可以用 LLM 做；本节用规则，因为**规则可复现、可测试**，
    而且能明确告诉你"哪些模式是我认得的"。
    """
    found: list[Constraint] = []
    seen: set[tuple[str, str]] = set()

    for message in messages:
        content = message.content

        for ticket in _TICKET_RE.findall(content):
            key = ("ticket", ticket)
            if key not in seen:
                seen.add(key)
                found.append(Constraint("ticket", ticket))

        for word in _APPROVAL_WORDS:
            if word in content:
                key = ("approval", word)
                if key not in seen:
                    seen.add(key)
                    found.append(Constraint("approval", content.strip()))
                break

        for match in _PROHIBIT_RE.finditer(content):
            phrase = match.group(0).strip()
            key = ("prohibit", phrase)
            if key not in seen:
                seen.add(key)
                found.append(Constraint("prohibit", phrase))

        for match in _MUST_RE.finditer(content):
            phrase = match.group(0).strip()
            key = ("must", phrase)
            if key not in seen:
                seen.add(key)
                found.append(Constraint("must", phrase))

    return found


def pin_constraints(messages: list[Message]) -> list[Message]:
    """给含约束的消息打上 pinned 标记（原地返回新列表）。"""
    constraints = extract_constraints(messages)
    risky = {item.text for item in constraints}
    risky_tickets = {item.text for item in constraints if item.kind == "ticket"}

    result: list[Message] = []
    for message in messages:
        hit = any(text and text in message.content for text in risky) or any(
            ticket in message.content for ticket in risky_tickets
        )
        result.append(Message(message.role, message.content, pinned=message.pinned or hit))
    return result


# ------------------------------ 压缩结果 ------------------------------


@dataclass
class CompressionResult:
    strategy: Strategy
    kept: list[Message]
    summary: str
    dropped: int
    tokens_before: int
    tokens_after: int
    constraints_before: list[Constraint] = field(default_factory=list)
    constraints_kept: list[Constraint] = field(default_factory=list)

    @property
    def lost_constraints(self) -> list[Constraint]:
        kept_texts = " ".join(message.content for message in self.kept) + " " + self.summary
        return [item for item in self.constraints_before if item.text not in kept_texts]

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "dropped": self.dropped,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "constraints_before": len(self.constraints_before),
            "constraints_lost": [f"{item.kind}:{item.text[:16]}" for item in self.lost_constraints],
            "summary_len": len(self.summary),
        }


# ------------------------------ 会话历史 ------------------------------


def _summarize(messages: list[Message], max_chars: int = 120) -> str:
    """确定性摘要：取每条的首句，拼到上限。

    **刻意做得"笨"**：真实的 LLM 摘要会更好，但也会**自信地丢掉细节**。
    用笨摘要来演示"摘要会丢约束"这件事，结论只会更保守。
    """
    parts: list[str] = []
    for message in messages:
        head = message.content.split("。")[0].strip()
        if head:
            parts.append(f"[{message.role}]{head}")
    text = "；".join(parts)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


class BudgetedHistory:
    """带 token 预算的会话历史。"""

    def __init__(self, budget_tokens: int = 400, keep_recent: int = 4) -> None:
        self.budget = budget_tokens
        self.keep_recent = keep_recent
        self.messages: list[Message] = []

    # -- 基本操作 --

    def add(self, role: str, content: str) -> Message:
        message = Message(role, content)
        self.messages.append(message)
        return message

    def pin_all_constraints(self) -> int:
        self.messages = pin_constraints(self.messages)
        return sum(1 for message in self.messages if message.pinned)

    def tokens(self) -> int:
        return sum(message.tokens for message in self.messages)

    def over_budget(self) -> bool:
        return self.tokens() > self.budget

    # -- 压缩 --

    def compress(self, strategy: Strategy) -> CompressionResult:
        before = self.tokens()
        constraints_before = extract_constraints(self.messages)

        if strategy == "window":
            kept, summary, dropped = self._window()
        elif strategy == "summary":
            kept, summary, dropped = self._summary()
        else:
            kept, summary, dropped = self._hybrid()

        self.messages = ([Message("system", summary)] if summary else []) + kept
        after = self.tokens()

        return CompressionResult(
            strategy=strategy,
            kept=list(kept),
            summary=summary,
            dropped=dropped,
            tokens_before=before,
            tokens_after=after,
            constraints_before=constraints_before,
        )

    # -- 三种策略的实现 --

    def _window(self) -> tuple[list[Message], str, int]:
        """只保留最近的，pinned 的也**不特殊对待**（这是它的弱点）。"""
        kept = self.messages[-self.keep_recent :]
        return kept, "", len(self.messages) - len(kept)

    def _summary(self) -> tuple[list[Message], str, int]:
        """把所有旧消息（**包括约束**）压成一句摘要。

        这是它的弱点：摘要器不知道哪句重要，只看"先出现什么"。
        """
        tail = self.messages[-self.keep_recent :]
        head = self.messages[: -self.keep_recent]
        summary = _summarize(head) if head else ""
        return tail, summary, len(head)

    def _hybrid(self) -> tuple[list[Message], str, int]:
        """约束全留 + 中段摘要 + 最近若干条原样。"""
        pinned = [message for message in self.messages if message.pinned]
        tail = [message for message in self.messages[-self.keep_recent :] if not message.pinned]
        middle = [
            message
            for message in self.messages[: -self.keep_recent]
            if not message.pinned
        ]
        summary = _summarize(middle) if middle else ""
        kept = pinned + tail
        return kept, summary, len(middle)
