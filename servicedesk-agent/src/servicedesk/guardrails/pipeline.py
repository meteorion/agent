# file: src/servicedesk/guardrails/pipeline.py
"""护栏：输入输出双向拦截。

和 5.3 的区别：

    5.3 是**事后评测**（跑完一批样本，看失败率）
    5.6 是**事中拦截**（这一条请求/答复，现在就处理）

三种处置动作（不是只有"拒绝"）：

    PASS     放行
    REWRITE  改写后放行（脱敏、补提示、去掉危险片段）
    BLOCK    拦下，返回体面话术

**为什么要有 REWRITE**：现实里大多数"问题输出"没必要整条毙掉。
"李娜的手机号是 138xxxx" 只需要脱敏；"答复里没标引用"只需要补一句提示。
**一刀切地拒绝，会让护栏本身变成用户体验问题。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

# ------------------------------- 判定 -------------------------------


class Verdict(str, Enum):
    PASS = "pass"
    REWRITE = "rewrite"
    BLOCK = "block"


@dataclass
class GuardResult:
    verdict: Verdict
    text: str                       # 放行/改写后的文本
    rule: str = ""
    reason: str = ""
    fired: list[str] = field(default_factory=list)   # 命中的规则（可能多条）

    @property
    def blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "rule": self.rule,
            "reason": self.reason,
            "fired": self.fired,
            "text": self.text,
        }


# ------------------------------- 规则模型 -------------------------------

Guard = Callable[[str, dict[str, Any]], GuardResult | None]


@dataclass(frozen=True)
class Rule:
    """一条规则：命中就返回 GuardResult，没命中返回 None。

    顺序有意义：**先 BLOCK 后 REWRITE**——安全规则优先，改写规则兜底。
    """

    name: str
    check: Guard
    description: str = ""


# ------------------------------- 输入规则 -------------------------------

MAX_INPUT_CHARS = 2000

_SENTENCE_RE = re.compile(r"[^。！？；\n]+")      # 注意用 findall 取"句子"，不是 split

# 注入类措辞（注意：只匹配"指令式"用法，不匹配正常讨论）
_INJECTION_PATTERNS = (
    re.compile(r"忽略(之前|以上|前面)[^。\n]{0,12}(指令|规则|设定)"),
    re.compile(r"(打印|输出|告诉我|展示)[^。\n]{0,8}(系统提示|system prompt)", re.I),
    re.compile(r"(你现在是|从现在起你是)[^。\n]{0,10}(管理员|root|开发者模式)"),
    re.compile(r"(跳过|绕过)[^。\n]{0,8}(审批|鉴权|权限校验)"),
)

# 危险请求：直接要求做高权限动作（而不是"问怎么做"）
_DANGEROUS_PATTERNS = (
    re.compile(r"(帮我|给我|直接)[^。\n]{0,10}(开|加|提升)[^。\n]{0,6}(管理员|root|最高权限)"),
    re.compile(r"(帮我把|直接)[^。\n]{0,10}密码[^。\n]{0,6}改成"),
    re.compile(r"(把|将)[^。\n]{0,10}(工单|P0)[^。\n]{0,8}(直接)?(关掉|关闭|删掉)"),
)


def _input_empty_or_too_long(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    stripped = text.strip()
    if not stripped:
        return GuardResult(Verdict.BLOCK, text, "input.empty", "输入为空，无法处理")
    if len(stripped) > MAX_INPUT_CHARS:
        return GuardResult(
            Verdict.BLOCK, text, "input.too_long",
            f"输入 {len(stripped)} 字，超过上限 {MAX_INPUT_CHARS}",
        )
    return None


# 请求式标记：有它才可能是"要求我做"
_IMPERATIVE = ("帮我", "给我", "直接", "麻烦你", "我要你", "立刻", "马上", "替我")
# 疑问式标记：问"能不能/是不是"通常不是指令
_INTERROGATIVE = ("吗", "是不是", "怎么", "如何", "为什么", "能否", "可不可以", "会不会")


def _looks_like_request(sentence: str) -> bool:
    """区分「要求我做」与「问能不能做」。

    实测教训（本节误杀了 1 条）：
        「上一个同事说可以跳过审批，是这样吗？」——这是**提问**，不是指令。
    判据：有请求式标记 → 算请求；只有疑问式标记 → 放行。
    这样"帮我跳过审批好吗？"仍然会被拦（有"帮我"），不能靠加个"吗"绕过。
    """
    if any(word in sentence for word in _IMPERATIVE):
        return True
    return not any(word in sentence for word in _INTERROGATIVE)


def _input_injection(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    for sentence in _SENTENCE_RE.findall(text) or [text]:
        if not _looks_like_request(sentence):
            continue
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(sentence)
            if match:
                return GuardResult(
                    Verdict.BLOCK, text, "input.injection",
                    f"疑似提示词注入：{match.group(0)[:20]}",
                )
    return None


def _input_dangerous_request(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    for sentence in _SENTENCE_RE.findall(text) or [text]:
        if not _looks_like_request(sentence):
            continue
        for pattern in _DANGEROUS_PATTERNS:
            match = pattern.search(sentence)
            if match:
                return GuardResult(
                    Verdict.BLOCK, text, "input.dangerous",
                    f"请求包含高权限动作，需走审批：{match.group(0)[:20]}",
                )
    return None


INPUT_RULES: list[Rule] = [
    Rule("input.empty", _input_empty_or_too_long, "空输入/超长"),
    Rule("input.injection", _input_injection, "提示词注入"),
    Rule("input.dangerous", _input_dangerous_request, "越权请求"),
]


# ------------------------------- 输出规则 -------------------------------

_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")

# 无依据承诺（软断言）
_PROMISE_WORDS = ("保证", "一定能", "百分之百", "100% 可以", "绝对不会")

# 危险指令（让用户去执行命令）
_DANGEROUS_COMMANDS = (
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"format\s+[a-zA-Z]:"),
    re.compile(r"(执行|运行)\s*(sudo|chmod\s+777)"),
)

# 系统提示词泄漏标志
_LEAK_MARKERS = ("系统提示：", "system prompt:", "你是 IT 服务台助手，可以调用工具")

# 拒绝类措辞：句子里同时出现"标志 + 拒绝"时不算失败（5.3 的教训）
_REFUSAL_WORDS = ("不能", "无法", "抱歉", "不会", "无权", "不便")

_CITATION_RE = re.compile(r"\[C\d+\]")


def _mask_pii(text: str) -> tuple[str, list[str]]:
    """把 PII 换成掩码，返回（新文本, 命中项）。"""
    hits: list[str] = []

    def _phone(match: re.Match[str]) -> str:
        value = match.group(0)
        hits.append(value)
        return value[:3] + "****" + value[-4:]

    def _id_card(match: re.Match[str]) -> str:
        value = match.group(0)
        hits.append(value)
        return value[:6] + "********" + value[-4:]

    return _ID_CARD_RE.sub(_id_card, _PHONE_RE.sub(_phone, text)), hits


def _output_pii(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    """PII 泄漏 → **脱敏后放行**，不是直接拒绝。"""
    masked, hits = _mask_pii(text)
    if not hits:
        return None
    return GuardResult(
        Verdict.REWRITE, masked, "output.pii",
        f"已脱敏 {len(hits)} 处敏感信息",
        fired=["output.pii"],
    )


def _sentences_without_disclaimer(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in _SENTENCE_RE.findall(text)
        if sentence.strip() and not any(word in sentence for word in _REFUSAL_WORDS)
    ]


def _output_promise(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    """无依据承诺 → 拦下（这类话术一旦说出口就是责任）。"""
    for sentence in _sentences_without_disclaimer(text):
        for word in _PROMISE_WORDS:
            if word in sentence:
                return GuardResult(
                    Verdict.BLOCK, text, "output.promise",
                    f"无依据承诺：{sentence[:24]}",
                )
    return None


def _output_dangerous_command(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    for pattern in _DANGEROUS_COMMANDS:
        match = pattern.search(text)
        if match:
            return GuardResult(
                Verdict.BLOCK, text, "output.dangerous_command",
                f"包含危险指令：{match.group(0)[:24]}",
            )
    return None


def _output_leak(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    for sentence in _sentences_without_disclaimer(text):
        for marker in _LEAK_MARKERS:
            if marker.lower() in sentence.lower():
                return GuardResult(
                    Verdict.BLOCK, text, "output.leak",
                    f"疑似泄漏内部提示：{sentence[:24]}",
                )
    return None


def _output_missing_citation(text: str, ctx: dict[str, Any]) -> GuardResult | None:
    """要求引用但没引用 → **补一句提示后放行**，而不是拒绝。"""
    if not ctx.get("require_citation"):
        return None
    if _CITATION_RE.search(text):
        return None
    patched = text.rstrip() + "\n\n（本答复未附资料出处，建议核实后再执行。）"
    return GuardResult(
        Verdict.REWRITE, patched, "output.missing_citation",
        "缺少引用标记，已追加核实提示",
        fired=["output.missing_citation"],
    )


OUTPUT_RULES: list[Rule] = [
    Rule("output.promise", _output_promise, "无依据承诺"),
    Rule("output.dangerous_command", _output_dangerous_command, "危险指令"),
    Rule("output.leak", _output_leak, "内部提示泄漏"),
    Rule("output.pii", _output_pii, "PII 泄漏（脱敏）"),
    Rule("output.missing_citation", _output_missing_citation, "缺少引用"),
]


# ------------------------------- 体面话术 -------------------------------

FALLBACK_REPLIES: dict[str, str] = {
    "input.empty": "请描述一下你遇到的问题，比如具体的报错或设备编号。",
    "input.too_long": "内容有点长，麻烦只保留问题和关键报错信息，我再帮你看。",
    "input.injection": "这个问题我没法按那种方式处理。你可以直接说遇到了什么 IT 问题。",
    "input.dangerous": "这类操作需要审批，我可以帮你发起审批流程。要现在提交吗？",
    "output.promise": "具体时效我无法保证，我会按流程尽快处理。",
    "output.dangerous_command": "这个操作有风险，我不能给出直接执行的指令。",
    "output.leak": "抱歉，内部配置信息不便提供。",
}


def fallback_reply(rule: str) -> str:
    """拦下之后给用户的话术——**不能只说"违反政策"**。

    好的话术三要素：说明为什么、给出下一步、不指责用户。
    """
    return FALLBACK_REPLIES.get(rule, "这个请求我处理不了，但我可以帮你走其它流程。")


# ------------------------------- 流水线 -------------------------------


@dataclass
class PipelineStats:
    checked: int = 0
    passed: int = 0
    rewritten: int = 0
    blocked: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    def record(self, result: GuardResult) -> None:
        self.checked += 1
        if result.verdict is Verdict.PASS:
            self.passed += 1
        elif result.verdict is Verdict.REWRITE:
            self.rewritten += 1
        else:
            self.blocked += 1
        for rule in result.fired or [result.rule]:
            if rule:
                self.by_rule[rule] = self.by_rule.get(rule, 0) + 1

    @property
    def block_rate(self) -> float:
        return self.blocked / self.checked if self.checked else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "passed": self.passed,
            "rewritten": self.rewritten,
            "blocked": self.blocked,
            "block_rate": round(self.block_rate, 3),
            "by_rule": dict(sorted(self.by_rule.items())),
        }


class GuardPipeline:
    """输入 → 处理 → 输出，两道闸。

    规则顺序：**BLOCK 优先**。一条请求只要命中任何 BLOCK 规则就停，
    不再往下试 REWRITE——因为"改写后放行"对一个明确该拒绝的请求没有意义。
    """

    def __init__(
        self,
        input_rules: Iterable[Rule] = INPUT_RULES,
        output_rules: Iterable[Rule] = OUTPUT_RULES,
    ) -> None:
        self.input_rules = list(input_rules)
        self.output_rules = list(output_rules)
        self.stats = PipelineStats()

    def check_input(self, text: str, **ctx: Any) -> GuardResult:
        result = self._run(self.input_rules, text, ctx)
        self.stats.record(result)
        return result

    def check_output(self, text: str, **ctx: Any) -> GuardResult:
        return self._run(self.output_rules, text, ctx)

    @staticmethod
    def _run(rules: list[Rule], text: str, ctx: dict[str, Any]) -> GuardResult:
        """规则**链式**执行：后一条看到的是前一条改写后的文本。

        为什么必须链式：实测教训——"联系 13800001234。" 同时命中
        PII 脱敏与缺引用补提示，若每条都基于原文，后一条会把脱敏结果覆盖掉。
        """
        patched = text
        fired: list[str] = []
        reasons: list[str] = []
        first_rewrite: GuardResult | None = None

        for rule in rules:
            outcome = rule.check(patched, ctx)
            if outcome is None:
                continue
            if outcome.verdict is Verdict.BLOCK:
                return outcome
            if first_rewrite is None:
                first_rewrite = outcome
            patched = outcome.text
            fired.extend(outcome.fired or [outcome.rule])
            reasons.append(outcome.reason)

        if first_rewrite is not None:
            return GuardResult(
                Verdict.REWRITE, patched, first_rewrite.rule,
                "；".join(dict.fromkeys(reasons)), fired=list(dict.fromkeys(fired)),
            )

        return GuardResult(Verdict.PASS, text)

    def safe_output(self, text: str, **ctx: Any) -> tuple[str, GuardResult]:
        """输出护栏的常用封装：返回"可以给用户看的文本" + 判定结果。"""
        result = self.check_output(text, **ctx)
        if result.verdict is Verdict.BLOCK:
            return fallback_reply(result.rule), result
        return result.text, result
