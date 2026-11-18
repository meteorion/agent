# file: src/servicedesk/logging_setup.py
"""结构化日志：让每条日志都带得上 trace_id 与关键字段。

为什么不用 print：
1. 线上要按 trace_id 把一次请求的所有日志串起来
2. 日志要能被机器解析（JSON），才能做"错误率""重试率"这类统计
3. 敏感字段（key、手机号）必须在写出去之前就被过滤
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(
    *, level: str = "INFO", json_logs: bool = False, stream: object | None = None
) -> None:
    """进程启动时调一次。

    json_logs=False 时用彩色控制台格式（本地开发）；
    json_logs=True 时输出单行 JSON（生产，交给采集系统）。

    `stream`：日志写到哪。**stdio 传输的 MCP Server 必须传 sys.stderr** ——
    因为 stdout 是协议通道，混进一行日志客户端就解析失败。
    """
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # httpx / httpcore 默认在 INFO 级别打「每一条请求」，
    # 开发时吵、生产时浪费存储。要排查就临时调到 DEBUG。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            # 从 contextvars 里捞 trace_id 等上下文，自动挂到每条日志上
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream)
        if stream is not None
        else structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(**fields: object) -> None:
    """给当前请求绑定上下文，之后所有日志都会带上它。

    典型用法：
        bind_request_context(trace_id="abc123", user_id="u-9")
        ...
        clear_request_context()   # 请求结束务必清掉，否则会串到下一个请求
    """
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str = "servicedesk") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
