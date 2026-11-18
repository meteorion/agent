# file: src/servicedesk/errors.py
"""异常分层：把"出错"拆成三种，让上层知道该做什么。

三种错误的处理方式完全不同：
- FatalError      → 别重试、别告警轰炸，这是代码/prompt 的问题，改完再发
- NeedsHumanError → 重试无用，但必须通知人（改配置、换 key、开权限）
- RetryableError  → 值得重试；重试用尽才抛出来

没有分层时，上层只能写 `except Exception: retry`，于是 401 被重试三次，
用户多等了 2 秒才看到"你的 key 无效"。
"""

from __future__ import annotations


class AgentError(Exception):
    """本项目所有「可预期错误」的基类。

    可预期 = 我们知道它会发生、知道怎么处理。
    不可预期（编程错误、内存溢出）不该被这个类捕获。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RetryableError(AgentError):
    """暂时性故障：429 限流、5xx、超时、网络抖动。

    抛出它意味着「已经重试过了，还是不行」。
    """

    def __init__(self, reason: str, *, attempts: int = 1) -> None:
        # base_reason 保留「不含尝试次数」的原始原因，日志里用它才不啰嗦
        self.base_reason = reason
        self.attempts = attempts
        super().__init__(f"{reason}（已尝试 {attempts} 次）")


class NeedsHumanError(AgentError):
    """需要人介入：key 无效、额度耗尽、权限不足。

    `hint` 是给运维看的那句话——写清楚"该去改什么"，
    否则半夜值班的人只能看到 401 然后开始猜。
    """

    def __init__(self, reason: str, *, hint: str = "") -> None:
        super().__init__(reason)
        self.hint = hint


class FatalError(AgentError):
    """重试没有意义的错误：请求格式错、字段缺失、资源不存在。

    这类错误 99% 是调用方代码或 prompt 的问题，重试只会重复犯错。
    """
