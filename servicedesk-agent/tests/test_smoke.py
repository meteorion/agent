# file: tests/test_smoke.py
"""冒烟测试：证明「包装对了、配置读对了」。

冒烟测试（smoke test）不是测业务逻辑，而是测「环境是不是活着」。
这三个用例都不依赖网络、不依赖 API Key。
"""

from __future__ import annotations

import pytest

from servicedesk import __version__
from servicedesk.config import Settings


def test_version_looks_like_semver() -> None:
    parts = __version__.split(".")
    assert len(parts) == 3, f"版本号应该是 x.y.z，实际是 {__version__}"
    assert all(p.isdigit() for p in parts)


def test_blank_api_key_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """空字符串必须被当成「没有 key」，而不是「有 key」。"""
    monkeypatch.setenv("LLM_API_KEY", "   ")
    settings = Settings(_env_file=None)  # 跳过 .env，只读当前环境变量
    assert settings.llm_api_key is None
    assert settings.has_llm_key is False


def test_missing_api_key_does_not_crash() -> None:
    """没有 key 时构造 Settings 必须成功（本节还不该要求 key）。"""
    settings = Settings(_env_file=None, llm_api_key=None)
    assert settings.has_llm_key is False
