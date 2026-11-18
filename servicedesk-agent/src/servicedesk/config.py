# file: src/servicedesk/config.py
"""全局配置：程序读取环境变量的唯一入口。

设计原则：
1. 除了这个文件，别处一律不许出现 os.environ / os.getenv。
2. 缺配置要在启动时就报错，而不是跑到一半才 500。
3. 密钥用 SecretStr 包起来，防止不小心被 print 或写进日志。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有可配置项的单一来源。

    字段名会被自动映射成大写环境变量名：
    app_env -> 读取环境变量 APP_ENV。
    """

    model_config = SettingsConfigDict(
        env_file=".env",           # 自动读取项目根目录的 .env
        env_file_encoding="utf-8",
        extra="ignore",            # .env 里多出来的键不报错（向前兼容）
        case_sensitive=False,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    # SecretStr：print() 时只会显示 '**********'，不会泄漏明文
    llm_api_key: SecretStr | None = None

    request_timeout_s: float = Field(default=30.0, gt=0, le=120)

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def _blank_string_to_none(cls, value: object) -> object:
        """把空字符串/纯空格统一变成 None。

        为什么需要它：.env.example 里的 LLM_API_KEY= 是空的，
        用户复制成 .env 后，环境变量的值就是空字符串 ""。
        如果直接写 llm_api_key: SecretStr | None = None，
        pydantic 会把 "" 当成一个合法的 SecretStr，于是
        has_llm_key 返回 True —— 程序一路跑到调 API 才 401。
        这是一个非常典型的「空字符串陷阱」。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def has_llm_key(self) -> bool:
        """是否配置了可用的 API Key。"""
        return self.llm_api_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例。

    同一个进程里，.env 只会被读取一次。
    测试时如果改了环境变量，需要调用 get_settings.cache_clear()。
    """
    return Settings()
