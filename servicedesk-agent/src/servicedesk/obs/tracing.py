# file: src/servicedesk/obs/tracing.py
"""W3C Trace Context：让一条请求的所有日志/跨度串得起来。

`traceparent` 就四个字段，用 `-` 分隔：

    traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
                 │  │                                │                │
                 │  │                                │                └─ flags（01=已采样）
                 │  │                                └─ parent-id（本跨度的父，16 hex）
                 │  └─ trace-id（整条链路唯一，32 hex）
                 └─ version（固定 00）

三个必须做对的地方：

    1. **透传**：接收方从 header 取，**沿用同一个 trace_id**，只换自己的 span_id
    2. **绑定**：把 trace_id 绑到日志上下文（否则日志里搜不到）
    3. **采样**：`-00` 表示不采样——**透传但不要记录**，别为了观测把生产压垮

⚠️ 一个常见误解：以为"改了 span_id 就是新 trace"。不是——**trace_id 必须一路不变**，
变的只是每一跳自己的 span_id 和它的 parent_id。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, MutableMapping

from servicedesk.logging_setup import bind_request_context

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
BAGGAGE_HEADER = "baggage"

_VERSION = "00"
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_FORBIDDEN = ("0" * 32, "0" * 16)


# ------------------------------- 上下文 -------------------------------


@dataclass(frozen=True)
class TraceContext:
    trace_id: str          # 32 hex，一路不变
    span_id: str           # 16 hex，本跳自己的
    parent_id: str | None = None
    sampled: bool = True

    def child(self) -> TraceContext:
        """派生下一跳：**trace_id 不变，span_id 换新，parent 指向我。**"""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=_new_span_id(),
            parent_id=self.span_id,
            sampled=self.sampled,
        )


def _new_span_id() -> str:
    return secrets.token_hex(8)


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def new_trace(*, sampled: bool = True) -> TraceContext:
    """发起一条新链路。"""
    return TraceContext(trace_id=_new_trace_id(), span_id=_new_span_id(), sampled=sampled)


# ------------------------------- 解析与格式化 -------------------------------


def parse_traceparent(header: str | None) -> TraceContext | None:
    """解析 `traceparent`。**任何不合规都返回 None**（而不是抛异常）。

    为什么要"宽容失败"：一个畸形 header 不该让整个请求 500。
    规范也说：**无法解析就当没收到，自己起一条新链路**。
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None

    version, trace_id, parent_id, flags = parts
    if version != _VERSION:
        return None
    if not _TRACE_ID_RE.match(trace_id) or trace_id in _FORBIDDEN:
        return None
    if not _SPAN_ID_RE.match(parent_id) or parent_id in _FORBIDDEN:
        return None
    try:
        flag_value = int(flags, 16)
    except ValueError:
        return None

    # 解析结果代表「上游的跨度」：它的 span_id 就是我们的 parent。
    # 本地要记录自己的跨度时，用 `ctx.child()` 派生——**不要直接拿它当自己**。
    return TraceContext(
        trace_id=trace_id,
        span_id=parent_id,
        parent_id=None,
        sampled=bool(flag_value & 0x01),
    )


def format_traceparent(ctx: TraceContext) -> str:
    """格式化回 header。

    `ctx` 是**要发出去的那一跳的父**（通常就是"我"自己的 span）。
    下游解析后会把它的 span_id 当作 parent。
    """
    flags = "01" if ctx.sampled else "00"
    return f"{_VERSION}-{ctx.trace_id}-{ctx.span_id}-{flags}"


def extract(headers: MutableMapping[str, str]) -> TraceContext | None:
    """从请求头取上下文（大小写不敏感）。"""
    lowered = {key.lower(): value for key, value in headers.items()}
    return parse_traceparent(lowered.get(TRACEPARENT_HEADER))


def extract_or_new(headers: MutableMapping[str, str]) -> TraceContext:
    """取上游上下文；没有就起一条新链路。

    **返回的是"上游跨度"**，本地要记录自己的跨度时请 `.child()`。
    """
    return extract(headers) or new_trace()


def inject(headers: MutableMapping[str, str], ctx: TraceContext) -> None:
    """把上下文塞进下游请求头。"""
    headers[TRACEPARENT_HEADER] = format_traceparent(ctx)


# ------------------------------- baggage -------------------------------


def parse_baggage(header: str | None) -> dict[str, str]:
    """`baggage: user_id=u-1,tenant=t-9` → dict。"""
    if not header:
        return {}
    result: dict[str, str] = {}
    for item in header.split(","):
        if "=" in item:
            key, _, value = item.partition("=")
            key = key.strip()
            if key:
                result[key] = value.strip()
    return result


def format_baggage(values: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in values.items())


# ------------------------------- 跨度 -------------------------------


@dataclass
class Span:
    name: str
    ctx: TraceContext
    started_at: float
    ended_at: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000 if self.ended_at else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.ctx.trace_id,
            "span_id": self.ctx.span_id,
            "parent_id": self.ctx.parent_id,
            "sampled": self.ctx.sampled,
            "started_at": round(self.started_at, 6),
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
            "events": self.events,
            "error": self.error,
        }


class Tracer:
    """极简 tracer：内存里收 span，可选落 JSONL（跨进程验证用）。"""

    def __init__(self, *, exporter: Path | None = None) -> None:
        self.exporter = exporter
        self.spans: list[Span] = []

    @contextmanager
    def span(self, name: str, ctx: TraceContext, **attributes: Any) -> Iterator[Span]:
        span = Span(name=name, ctx=ctx, started_at=time.time(), attributes=dict(attributes))
        try:
            yield span
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.ended_at = time.time()
            self._finish(span)

    def _finish(self, span: Span) -> None:
        # 采样为 false 时不记录——**透传但不算开销**
        if not span.ctx.sampled:
            return
        self.spans.append(span)
        if self.exporter is not None:
            self.exporter.parent.mkdir(parents=True, exist_ok=True)
            with self.exporter.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(span.as_dict(), ensure_ascii=False) + "\n")

    def bind_logs(self, span: Span) -> None:
        """把 trace/span 绑到日志上下文，之后所有日志自动带上。"""
        bind_request_context(trace_id=span.ctx.trace_id, span_id=span.ctx.span_id)


# ------------------------------- 读回与还原 -------------------------------


def load_spans(path: str | Path) -> list[dict[str, Any]]:
    """从 JSONL 读回 span（跨进程验证时用）。"""
    file = Path(path)
    if not file.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 span 列表还原成调用树：按 parent_id 挂靠，按开始时间排序。

    **找不到父的 span 会被当成根**（跨进程时某一跳没落盘，这是常态，不能因此丢数据）。
    """
    by_id = {span["span_id"]: dict(span, children=[]) for span in spans}
    roots: list[dict[str, Any]] = []

    for span in by_id.values():
        parent = by_id.get(span.get("parent_id") or "")
        if parent is None:
            roots.append(span)
        else:
            parent["children"].append(span)

    def sort_tree(nodes: list[dict[str, Any]]) -> None:
        nodes.sort(key=lambda item: item["started_at"])
        for node in nodes:
            sort_tree(node["children"])

    sort_tree(roots)
    return roots


def render_tree(roots: list[dict[str, Any]], *, indent: int = 0) -> list[str]:
    """把调用树画成缩进文本，方便贴进日志或报告。"""
    lines: list[str] = []
    for node in roots:
        prefix = "  " * indent + ("└─ " if indent else "")
        error = f"  ⚠ {node['error']}" if node.get("error") else ""
        lines.append(f"{prefix}{node['name']}  ({node['duration_ms']:.1f}ms){error}")
        lines.extend(render_tree(node["children"], indent=indent + 1))
    return lines


def exporter_path() -> Path | None:
    """从环境变量读导出路径（子进程用它落盘）。"""
    raw = os.environ.get("TRACE_LOG")
    return Path(raw) if raw else None
