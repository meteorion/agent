# file: tests/conftest.py
"""pytest 全局配置。

这里只做一件事：把 Windows 上"客户端提前断连"的噪音压掉。
它不是被测代码的 bug，但会把失败时的堆栈冲得看不清。
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True, scope="session")
def _quiet_asyncio_noise() -> None:
    # ProactorEventLoop 在连接被对端重置时，会经 asyncio logger 打一条 ERROR
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    yield
