---
section: "1.1"
title: "从空目录建立可运行的工程骨架"
stage: 1
status: done
depth: L2
difficulty: 1
minutes: 45
milestone: M1
depends_on: []
produces: ["pyproject.toml", "src/servicedesk/config.py", "src/servicedesk/__main__.py"]
verifies: "删掉 .venv 后一条 uv sync 能重建并跑通"
part: null
updated: "2026-11-17"
---

# 1.1 从空目录建立可运行的工程骨架

## 0. 本节导航

**本节在链路中的位置**：整条教程的起点。后面 36 节的所有代码都会加进本节创建的这个仓库，不会另起项目。前面没有内容，后面接 **1.2 async 心智模型**（本节只搭仓库，不写业务逻辑）。

**本节你将获得**

1. 用两条命令建立一个符合 `src` 布局、`import` 不会出错的 Python 包。
2. 用 `uv` 锁住依赖，换台机器（或三个月后的你）能一条命令复现完整环境。
3. 建立 `.env` 与 `.env.example` 双文件机制，让密钥永远进不了 git。
4. 写出可运行入口 `python -m servicedesk`，它自己会告诉你环境配置对不对。
5. 写下第一个 `pytest` 用例，用断言证明"装对了"。

**前置检查清单**（1 分钟）

- 能打开 PowerShell，会用 `cd` 进目录。
- 不需要预装 Python、不需要 pip、不需要 API Key。
- 有一个能编辑文本的编辑器（VS Code 即可）。

**难度**：★☆☆☆☆ ｜ **预计时长**：45 分钟 ｜ **本节费用**：**¥0**（一次 LLM API 都不调）

---

## 1. 为什么需要它（痛点先行）

假设你按某篇博客敲下 `pip install langchain`，跑通了一个 demo，感觉自己入门了。三个月后换了台电脑想再跑一遍：

```
ImportError: cannot import name 'StateGraph' from 'langchain'
```

你没改过任何代码。变的是**当时装的版本和今天装上的版本不是同一个**：`pip install` 不指定版本就是装"当前最新"，而库的大版本升级会直接删掉旧 API。

再看另外两个高频事故：

**事故 A：`import` 找不到自己的模块**

```
ModuleNotFoundError: No module named 'servicedesk'
```

这是初学者最难查的一类错，因为它和"代码写错了"长得一样，实际是"目录结构让 Python 找错地方了"。

**事故 B：密钥被推上 GitHub**

`.env` 里写着 `sk-proj-xxxx`，某次手滑 `git add -A && git push`。第二天收到邮件说 key 已泄露，额度被刷。

**这三个事故是同一类问题：它们都在你写第一行业务代码之前就已经决定了。** 工程骨架不是仪式感，它是提前关掉这三个风险开关：

| 事故 | 对应的开关 |
|---|---|
| 依赖版本漂移 | `pyproject.toml` 写版本区间 + `uv.lock` 锁精确版本 |
| `import` 找错地方 | `src` 布局（2.5 讲清原理） |
| 密钥泄漏 | `.gitignore` 先于第一次 commit 建立 |

---

## 2. 核心概念与架构设计

### 2.1 一个类比：装修房子

| 工程里的东西 | 类比 | 谁读它 |
|---|---|---|
| `pyproject.toml` | **房屋图纸 + 材料清单**（几袋水泥、什么标号） | 人读，也要机器读 |
| `uv.lock` | **实际采购单**（精确到每颗螺丝的批次） | 机器读 |
| `.venv/` | **你家实际堆材料的仓库** | Python 解释器读 |
| `src/servicedesk/` | **房间隔断**（哪间是卧室） | Python 解释器读 |
| `.env` | **门禁卡**（只给你自己） | 你的程序读 |

> **这个类比在哪失效**：图纸（`pyproject.toml`）改了，房子**不会自动重建**。你必须显式执行 `uv sync` 才会按新图纸重新采购。所以后面你会反复看到"改完 `pyproject.toml` 一定要 `uv sync`"——因为这是唯一需要你手动记住的动作。

### 2.2 目录结构全景

```
servicedesk-agent/            ← 项目根（你执行所有命令的地方）
├── pyproject.toml            ← 图纸 + 材料清单（进 git）
├── uv.lock                   ← 采购单，精确版本（进 git）
├── .python-version           ← 指定用哪个 Python 小版本（进 git）
├── .env.example              ← 配置模板，不含真实值（进 git）
├── .env                      ← 你的真实密钥（不进 git）
├── .venv/                    ← 材料仓库（不进 git）
├── src/
│   └── servicedesk/          ← 你的包本体（必须先安装才能 import）
│       ├── __init__.py
│       ├── config.py
│       └── __main__.py
└── tests/
    └── test_smoke.py
```

### 2.3 一次 `uv run` 背后发生了什么

```
你写 pyproject.toml
        │
        │  uv sync
        ▼
┌─────────────────────────────────────┐
│ 1. 读 pyproject.toml 的版本区间      │
│ 2. 按 uv.lock 解析出精确版本         │
│ 3. 缺 Python 就自动下载              │
│ 4. 创建 .venv/                       │
│ 5. 装第三方依赖                      │
│ 6. 以「可编辑模式」装上你自己的包 ────┼──► import 成功的关键
└─────────────────────────────────────┘
        │
        │  uv run python -m servicedesk
        ▼
   uv 确认环境最新 → 在 .venv 里执行 → 打印自检信息
```

### 2.4 逐层拆解：四个部件各管什么

**① `pyproject.toml` —— 唯一真相来源**
同时承担五件事：包名与版本、依赖版本区间、构建后端（怎么打成 wheel）、dev 依赖组、工具配置（pytest / ruff）。它取代了以前的 `setup.py` + `requirements.txt` + `setup.cfg`。

**② `uv.lock` —— 让"我的机器上能跑"变成"任何机器上都能跑"**
`pyproject.toml` 写的是 `pydantic>=2.9,<3.0`（区间，允许浮动）；`uv.lock` 记录 `pydantic==2.14.3`（确定值）。前者表达意图，后者保证复现。

**③ `.venv/` —— 隔离的仓库**
它让本项目的依赖不与别的项目打架。**永远不进 git**：它能用 `uv sync` 重新生成，而且里面有上千个文件。

**④ 构建后端（本节用 hatchling）—— 让人知道你的包在哪**
`[tool.hatch.build.targets.wheel] packages = ["src/servicedesk"]` 是在说："`src/servicedesk` 这个目录就是要装进环境的包。"**这一行漏写，就是 6.1 实验里那个报错。**

### 2.5 为什么必须用 `src/` 布局（**本节最反直觉的地方**）

Python 有个隐藏行为：**执行 `python xxx.py` 时，当前所在目录会被自动塞进 `sys.path`**（模块搜索路径最前面）。

于是如果你的代码直接放根目录：

```
servicedesk-agent/
├── servicedesk/        ← 包就在根目录
│   └── __init__.py
└── tests/
```

那么你在项目根跑 `pytest` 时，`import servicedesk` **会直接导入根目录那个文件夹**，根本不走"已安装的包"，你甚至不用 `uv sync` 也能 import 成功。

后果：**本地能跑，CI 挂**。因为你从没验证过"包真的被正确打包安装"这件事。等发布到服务器，`import` 立刻崩，而你在本地复现不出来。

`src/` 布局的作用是：把包藏进一个不参与 `sys.path` 的目录，**逼你必须先正确安装，才能 import 成功**。这样"本地跑通"和"CI 跑通"才是同一件事。

> **边界**：单个脚本、Jupyter notebook、一次性数据分析**不需要**这套布局，平铺更省事。这套约束只在你要交付、要被人用、要长期维护时才值得。

---

## 3. 环境与版本（复制粘贴级）

### 3.1 安装 uv（Windows PowerShell）

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

装完**关掉当前终端再开一个新的**（让 PATH 生效），然后验证：

```powershell
uv --version
```

实测输出：

```
uv 0.11.31 (b7fdec626 2026-07-21 x86_64-pc-windows-msvc)
```

> ⚠️ 版本号会不同，0.9 以上都满足本节要求。uv 的 CLI 在 2024–2026 间调整过若干参数，所以本节刻意**只用 `uv init --bare` + 手写全部文件**，不依赖生成器的默认行为。若你的版本连 `--bare` 都不认，**直接手动新建 `pyproject.toml`**，效果完全一样。

### 3.2 本节验证过的版本组合

| 组件 | 实测版本 | 说明 |
|---|---|---|
| uv | `0.11.31` | 低于 0.8 可能不支持 `[dependency-groups]` |
| Python | `3.12.13` | 由 `uv python pin 3.12` 固定 |
| pydantic | `2.14.3` | v2 与 v1 API 不兼容，**不要装 v1** |
| pydantic-settings | `2.16.3` | 负责从 `.env` 读配置 |
| python-dotenv | `1.2.4` | `.env` 解析的底层实现 |
| pytest | `8.4.2` | — |
| pytest-asyncio | `1.5.0` | 本节不用，1.2 起要用 |
| ruff | `0.17.5` | 代码检查 |

> 本节的依赖清单**只含本节真正用到的包**。`structlog`、`tenacity`、`httpx` 会在 1.5 / 1.3 用到时再 `uv add`——那时你会亲手体验一次"改完 pyproject 必须 sync"。

### 3.3 建立项目

```powershell
mkdir servicedesk-agent
cd servicedesk-agent
uv init --bare
uv python pin 3.12
```

实测输出：

```
Pinned `.python-version` to `3.12`
```

此时目录里只有 `pyproject.toml` 和 `.python-version`。接下来 7 个文件全部手写（3.4 给出），不依赖生成器。

---

## 4. 代码实战（5 个代码文件 + 2 个配置文件，全部可直接运行）

> 每个代码块首行标注了文件路径，**路径就是你要创建的位置**。这些文件已实机跑通，见 §7 的真实输出。

### 文件 1 / 7：`pyproject.toml`

```toml
# file: pyproject.toml
[project]
name = "servicedesk-agent"
version = "0.1.0"
description = "企业 IT 工单智能体（AI Agent 零基础到生产级 主线项目）"
requires-python = ">=3.12,<3.14"
dependencies = [
    "pydantic>=2.9,<3.0",
    "pydantic-settings>=2.5,<3.0",
    "python-dotenv>=1.0,<2.0",
]

# 开发依赖：不进生产环境。uv run 默认会加载 dev 组
[dependency-groups]
dev = [
    "pytest>=8.3,<9.0",
    "pytest-asyncio>=0.24,<2.0",
    "ruff>=0.6,<1.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

# 关键一行：告诉打包工具，要装进环境的包在 src/servicedesk
[tool.hatch.build.targets.wheel]
packages = ["src/servicedesk"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
asyncio_mode = "auto"
# 消掉 pytest-asyncio 的 DeprecationWarning，不是必须的
asyncio_default_fixture_loop_scope = "function"

[tool.ruff]
line-length = 100
target-version = "py312"
```

### 文件 2–3 / 7：`.gitignore` 与 `.env.example`

```gitignore
# file: .gitignore
.venv/
.env
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
dist/
build/
*.egg-info/
data/
```

```ini
# file: .env.example
# 用法：复制一份改名为 .env，再填真实值
#   PowerShell:  Copy-Item .env.example .env
#   macOS/Linux: cp .env.example .env
APP_ENV=dev
LOG_LEVEL=INFO
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
# 先留空。1.3 之前不需要 key；留空时程序必须能正常启动
LLM_API_KEY=
REQUEST_TIMEOUT_S=30
```

### 文件 4 / 7：`src/servicedesk/__init__.py`

```python
# file: src/servicedesk/__init__.py
"""servicedesk：企业 IT 工单智能体。

这个文件的存在本身就是有意义的：它让 Python 把 src/servicedesk
识别为一个「包」（package），而不是一个普通文件夹。
"""

__version__ = "0.1.0"
```

### 文件 5 / 7：`src/servicedesk/config.py`

```python
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
```

### 文件 6 / 7：`src/servicedesk/__main__.py`

```python
# file: src/servicedesk/__main__.py
"""模块入口：uv run python -m servicedesk 会执行这里的 main()。

它的作用是「环境自检」—— 在你还没写业务代码时，
先用三十行代码确认：包能导入、配置能读、Python 版本对。
"""

from __future__ import annotations

import sys

from servicedesk import __version__
from servicedesk.config import get_settings


def main() -> int:
    settings = get_settings()
    line = "=" * 48

    print(line)
    print(f"servicedesk-agent v{__version__}")
    print(f"python       : {sys.version.split()[0]}")
    print(f"executable   : {sys.executable}")
    print(f"app_env      : {settings.app_env}")
    print(f"llm_base_url : {settings.llm_base_url}")
    print(f"llm_model    : {settings.llm_model}")
    print(f"timeout      : {settings.request_timeout_s}s")
    key_state = "已配置" if settings.has_llm_key else "未配置（1.3 之前不需要）"
    print(f"api_key      : {key_state}")
    print(line)

    if sys.version_info < (3, 12):
        print("✗ Python 版本过低，需要 3.12 及以上", file=sys.stderr)
        return 1

    print("✓ 环境自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### 文件 7 / 7：`tests/test_smoke.py`

```python
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
```

### 安装并运行

```powershell
uv sync
Copy-Item .env.example .env
uv run python -m servicedesk
```

---

## 5. 逐块拆解

### 5.1 `pyproject.toml`：为什么写区间不写死

```toml
"pydantic>=2.9,<3.0",
```

- **下界 `>=2.9`**：低于它，`field_validator` 的某些行为不一致。
- **上界 `<3.0`**：pydantic 3 一定会改 API，锁大版本，避免某天 `uv sync` 把你从"能跑"同步成"不能跑"。
- **为什么不写 `==2.14.3`**：写死会让安全补丁也装不进来，而且多个库会互相锁死（A 要 2.9、B 要 2.14，你就解不出来了）。

**精确版本交给 `uv.lock`，`pyproject.toml` 只表达"我要什么范围"。这是两者的分工。**

### 5.2 `packages` 这一行在干嘛

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/servicedesk"]
```

hatchling 默认按**项目名**猜包目录。项目名是 `servicedesk-agent`，它会去找 `servicedesk_agent/` 或 `src/servicedesk_agent/`——**都不存在**，于是打包失败。显式写 `src/servicedesk` 等于告诉它："别猜了，包在这儿。"**改掉它会怎样，6.1 实验演示。**

### 5.3 为什么 `llm_api_key` 要套一层 `SecretStr`

普通 `str` 的问题：任何一次 `print(settings)`、任何一次日志带上了 settings、任何一次报错堆栈里带了对象，密钥就进了日志或监控平台。`SecretStr` 让它在被打印时自动显示 `**********`；拿明文必须显式写 `.get_secret_value()`——**这个"显式"动作就是你的安全边界**，代码评审时搜这个词就能定位所有拿明文的地方。

### 5.4 那个 `_blank_string_to_none` 校验器（本节知识点）

先看**没有它会发生什么**。

`.env.example` 里是 `LLM_API_KEY=`（等号右边什么都没有）。用户复制成 `.env` 后，`pydantic-settings` 读到的值就是**空字符串 `""`**，不是 `None`。

而 `SecretStr | None` 在 pydantic 里的含义是"要么是 SecretStr，要么是 None"。空字符串**是一个合法的字符串**，于是被包成了 `SecretStr("")`。

实测（6.2 实验的等价代码）：

```
repr      : SecretStr('')
has_key   : True          ← 谎言
```

程序于是认为"我有 key"，一路跑到真正发 HTTP 请求才收到 `401`。**错误被推迟到了离根因最远的地方**，你会去查网络、查代理、查 key 格式，唯独不会怀疑"key 其实是空的"。

加上 `mode="before"` 的校验器，就在**进入模型之前**把 `""` 归一成 `None`，让 `has_llm_key` 说真话。

> **每节一个"啊哈点"：空字符串永远是谎言。** 配置层必须把 `""` / `"   "` / `None` 统一成一种语义，否则这个错误一定在最贵的地方爆出来。

### 5.5 `extra="ignore"` 不是可选项

`SettingsConfigDict` 里这行容易被当成样板代码删掉：

```python
extra="ignore",
```

删掉的后果我实测过：只要 `.env` 里出现一个 `Settings` 没声明的键，构造对象时直接炸：

```
pydantic_core._pydantic_core.ValidationError: 5 validation errors for Naive
app_env
  Extra inputs are not permitted [type=extra_forbidden, input_value='dev', input_type=str]
```

pytest-asyncio 报错还算是好事。真正麻烦的是**团队协作**：别人在 `.env` 里加了个 `HTTP_PROXY`，你的程序启动就崩，而错误信息指向的是"app_env 不允许"，你会查半天配置字段名。`extra="ignore"` 让未知键被忽略——这符合"配置向前兼容"的工程直觉。

### 5.6 为什么入口用 `__main__.py` 而不是根目录 `main.py`

`python -m servicedesk` 让 Python 去找 `servicedesk` 包里的 `__main__.py` 并执行。

好处是：**它强制走"包已安装"的路径**。如果写成根目录 `main.py` + `python main.py`，你又回到了"当前目录进 sys.path"的老问题，`src` 布局就白搭了。

### 5.7 `lru_cache` 包 `get_settings` 的代价

`.env` 读一次就够。每个函数都 `Settings()` 会频繁读盘，且同一进程里可能出现两份不一致的配置。

`@lru_cache(maxsize=1)` 让它成为进程级单例。**代价**是：测试里改了环境变量后必须 `get_settings.cache_clear()`，否则读到旧值。这个副作用 1.5 会正面处理。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：注释掉 `packages` 那一行

把 `pyproject.toml` 里这两行注释掉：

```toml
# [tool.hatch.build.targets.wheel]
# packages = ["src/servicedesk"]
```

执行：

```powershell
uv sync
```

实测真实报错（节选）：

```
   Building servicedesk-agent @ file:///E:/WorkSpace/personal/agent/servicedesk-agent
  × Failed to build `servicedesk-agent @ file:///...`
  ├─▶ The build backend returned an error
  ╰─▶ Call to `hatchling.build.build_editable` failed (exit code: 1)
      ValueError: Unable to determine which files to ship
      inside the wheel using the following heuristics:
      ...
      The most likely cause of this is that there is no directory that matches
      the name of your project (servicedesk_agent).

      At least one file selection option must be defined
      in the `tool.hatch.build.targets.wheel` table
```

**人话翻译**：打包工具按项目名 `servicedesk_agent` 找目录，找不到，罢工。

此时再跑模块：

```powershell
uv run python -m servicedesk
```

```
  × Failed to build `servicedesk-agent @ file:///...`
```

**注意这个连锁反应**：一个打包配置错了，报出来的却是"构建失败"，方向被带偏了一半。这正是初学者最需要训练的能力——遇到第二个错误时，先回头看 `uv sync` 到底成功没有。

**修法**：取消注释，重跑 `uv sync`。

### 6.2 实验 B：删掉空字符串校验器

把 `config.py` 里 `_blank_string_to_none` 整个方法删掉（含上面的 `@field_validator` 装饰器），确认 `.env` 里是空的：

```powershell
Select-String -Path .env -Pattern "LLM_API_KEY"
```

```
.env:6:LLM_API_KEY=
```

运行：

```powershell
uv run python -m servicedesk
```

```
api_key      : 未配置（1.3 之前不需要）     ← 改坏前，说真话
api_key      : 已配置                       ← 改坏后，说谎
```

**注意：程序没有崩，它只是安静地骗了你。** 这类 bug 在生产里最贵——因为它不报警，只会在真正调用时变成 401、空回答、或莫名其妙的账单。

---

## 7. 验证与预期输出

以下是本机实测（`uv 0.11.31` / Windows 11 / Python 3.12.13）。

### 7.1 装依赖

```powershell
uv sync
```

```
Installed 16 packages in 1.49s
 + pydantic==2.14.3
 + pydantic-settings==2.16.3
 + pytest==8.4.2
 + pytest-asyncio==1.5.0
 + python-dotenv==1.2.4
 + ruff==0.17.5
 + servicedesk-agent==0.1.0 (from file:///.../servicedesk-agent)
 ...
```

**注意最后一行**：你自己的包也被装进去了（可编辑模式）。没有这一行，后面的 `import` 一定失败。

### 7.2 自检入口

```powershell
Copy-Item .env.example .env
uv run python -m servicedesk
```

```
================================================
servicedesk-agent v0.1.0
python       : 3.12.13
executable   : E:\WorkSpace\personal\agent\servicedesk-agent\.venv\Scripts\python.exe
app_env      : dev
llm_base_url : https://api.openai.com/v1
llm_model    : gpt-4.1-mini
timeout      : 30.0s
api_key      : 未配置（1.3 之前不需要）
================================================
✓ 环境自检通过
```

检查退出码：

```powershell
$LASTEXITCODE
```

```
0
```

### 7.3 跑测试

```powershell
uv run pytest
```

```
...                                                                      [100%]
3 passed in 0.99s
```

**看到 `3 passed` 本节才算完成。** 注意这 3 个测试都不依赖网络、不依赖 Key——这是刻意的：你的 CI 不会因为"今天没网"变红。

### 7.4 验收标准：删掉 `.venv` 能重建

```powershell
Remove-Item -Recurse -Force .venv
uv sync
uv run python -m servicedesk
```

实测最后两行：

```
================================================
✓ 环境自检通过
```

**这就是本节 toc 里那条验收标准**：`.venv` 是可丢弃的，真正重要的是 `pyproject.toml` + `uv.lock`。

### 7.5 验证 `src` 布局真的生效

```powershell
cd C:\
uv --project E:\WorkSpace\personal\agent\servicedesk-agent run python -c "import servicedesk; print('import OK:', servicedesk.__version__)"
```

实测：

```
import OK: 0.1.0
```

**能在项目外的任意目录 import 成功，说明包是"装进去"的，不是"碰巧在当前目录"。** 这就是 `src` 布局向你保证的东西。

### 7.6 可写进 CI 的断言

```powershell
uv run python -c "from servicedesk.config import Settings; s=Settings(_env_file=None); assert s.app_env in ('dev','test','prod'); print('config OK')"
```

---

## 8. 生产环境踩坑实录

### 坑 1：命令跑了，什么都没输出，退出码 1

**现象**

```powershell
python .dsh/skills/agent-tutorial/scripts/tutorial.py status
```

一行输出都没有，`$LASTEXITCODE` 是 `1`。

**定位方法**

```powershell
Get-Command python | Select-Object Source
```

```
Source
------
C:\Users\meteor\AppData\Local\Microsoft\WindowsApps\python.exe
```

`WindowsApps` 这一层是 **Microsoft Store 的应用执行别名**。系统里没装真 Python 时，这个假 shim 会静默失败——不报错、不提示、直接退出 1。

**根因**

Windows 在 `PATH` 前置了 Store 别名目录；真正的 Python 可能压根没装，也可能装在别处但被这个别名遮住。

**修法**（三选一，按推荐顺序）

```powershell
# 1) 用 uv 提供的解释器，绕开 PATH 问题（推荐）
uv run --no-project python your_script.py

# 2) 确认 uv 里有哪些可用解释器
uv python list

# 3) 关掉 Store 别名：设置 → 应用 → 高级应用设置 → 应用执行别名 → 关掉 python.exe / python3.exe
```

**顺手加固**：脚本里别只靠"没有输出"判断成功，永远检查退出码；CI 里用 `uv run python` 而不是裸 `python`。

### 坑 2：`ModuleNotFoundError: No module named 'servicedesk'`

**现象**

```
ModuleNotFoundError: No module named 'servicedesk'
```

刚才还能跑。

**定位方法**（两步分离根因）

```powershell
uv pip show servicedesk-agent
```

```powershell
uv run python -c "import sys; print(sys.prefix)"
```

- 第一条报 `Package(s) not found` → 包根本没装。
- 第二条打印的是系统 Python 路径、而不是项目里的 `.venv` → 你不在项目环境里。

**根因**（按频率排序）

1. 改完 `pyproject.toml` 没执行 `uv sync`。
2. 在 `src/` 目录里执行命令，工作目录变成了包内部。
3. 漏建 `__init__.py`，Python 不认它是包。

**修法**

```diff
- PS E:\...\servicedesk-agent\src> uv run python -m servicedesk
+ PS E:\...\servicedesk-agent> uv run python -m servicedesk
```

```powershell
uv sync --reinstall-package servicedesk-agent
```

**顺手加固**：在 `tests/` 里断言包路径来自已安装位置，让这类问题在 CI 就被拦住。

### 坑 3：`.env` 被提交进了 git

**现象**

收到 "Your API key has been leaked" 邮件，或账单出现不认识的调用。

**定位方法**

```powershell
git log --all --full-history --oneline -- .env
git check-ignore -v .env
```

预期第二条有输出：

```
.gitignore:2:.env    .env
```

**没有输出**就说明 `.gitignore` 没生效。

**根因**

1. `.gitignore` 是在第一次 commit **之后**才建的。git 对**已跟踪**文件不再看 `.gitignore`。
2. 用了 `git add -A` / `git add .`，在规则生效前把 `.env` 加进了暂存区。

**修法**（顺序不能颠倒）

第一步，**立刻吊销旧 key** 并在控制台生成新 key。只要 key 进过远端就必须假定已公开，改历史不能撤销泄漏。

第二步，停止跟踪但不删本地文件：

```powershell
git rm --cached .env
git commit -m "chore: stop tracking .env"
```

```diff
- git add -A            # 危险：会把 .env 也加进去
+ git add -A --dry-run  # 先看要加什么
+ git status --short    # 确认 .env 不在列表里再提交
```

第三步（可选）清理历史，需先装 `git-filter-repo`：

```powershell
git filter-repo --path .env --invert-paths
```

> ⚠️ 这会重写全部 commit hash，**必须通知所有协作者重新克隆**。个人项目且 key 已吊销的话可以跳过——旧 key 已是废字符串，历史里留着没有实际风险。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么包要放在 `src/` 下面，而不是直接放根目录？</b></summary>

因为 Python 执行时会把**当前目录**加入 `sys.path`。包放根目录时，`import servicedesk` 命中那个文件夹本身而不是"已安装的包"，于是不 `uv sync` 也能 import 成功，掩盖打包配置错误，造成"本地能跑、CI/服务器挂"。放 `src/` 后必须安装才能 import，本地与 CI 行为一致。

</details>

<details>
<summary><b>Q2：`uv.lock` 要不要提交进 git？</b></summary>

**要。** 它是复现构建的关键：`pyproject.toml` 写区间（意图），`uv.lock` 锁精确版本（结果）。不提交则每人 `uv sync` 解出的版本可能不同，"我这儿能跑"永远无法验证。唯一例外是你写一个要给别人 `pip install` 的库——那时 lock 不进发行包，但仍建议提交进仓库。

</details>

<details>
<summary><b>Q3：`requires-python` 和 `.python-version` 有什么区别？</b></summary>

- `requires-python = ">=3.12,<3.14"` 是**约束**：声明这个包在哪些 Python 上能工作，解析依赖时用它筛版本。
- `.python-version`（`uv python pin 3.12` 生成）是**选择**：告诉 uv"请用 3.12 这个具体小版本"，缺了会自动下载。

两者配合：约束划出可接受范围，pin 在范围内选一个确定的点，保证团队拿到同一个解释器。

</details>

### 9.2 动手任务：给入口加 `--check` 自检模式

**要求**

1. `uv run python -m servicedesk --check` 只输出三行检查结果，每行带 `✓` 或 `✗`：
   - Python 版本 ≥ 3.12
   - 依赖可导入（`pydantic`、`pydantic_settings`）
   - `.env` 文件存在（用 `pathlib.Path(".env").exists()`，不要用 `os.path`）
2. 三项全过 → 退出码 `0`；任一失败 → 退出码 `1`。
3. 不带参数时保持现在的完整自检输出。
4. 新增 `tests/test_cli_check.py`，覆盖"失败时退出码为 1"。

**验收标准（做到什么程度算通过）**

- 正常状态：`uv run python -m servicedesk --check; $LASTEXITCODE` → 三行 `✓`，退出码 `0`。
- 把 `.env` 临时改名成 `.env.bak` 再跑：第三行变 `✗`，退出码 `1`，且**不抛异常堆栈**。
- `uv run pytest` 全绿，且新测试确实在"`.env` 不存在"分支上红过一次。

**提示**：用标准库 `argparse`，**不要**为图快装 click/typer——本节目标是理解入口，不是学 CLI 框架。

### 9.3 拓展挑战

在 `pyproject.toml` 加：

```toml
[project.scripts]
servicedesk = "servicedesk.__main__:main"
```

`uv sync` 后验证 `uv run servicedesk` 与 `uv run python -m servicedesk` 效果一致。

**思考**：两种入口的 `sys.argv[0]` 不同，会不会影响你的 `--check` 实现？如果会，怎么让它在两种入口下都工作？

---

## 10. 本节小结与延伸

**三点回顾**

1. **`pyproject.toml` 写意图（区间），`uv.lock` 锁结果（精确版本），`.venv` 是可丢弃的仓库**——三者分工明确，`.venv` 与 `.env` 永不进 git。
2. **`src` 布局是为了让"本地能跑"和"CI 能跑"成为同一件事**；它的代价（必须先正确安装才能 import）正是它给你的保障。
3. **空字符串是谎言的源头**：配置层要把 `""` / 空格 / `None` 统一语义，密钥用 `SecretStr` 包住，拿明文必须是显式动作。

**延伸阅读**

- uv 官方文档（项目初始化、依赖组）：https://docs.astral.sh/uv/
- Python 官方打包指南 `pyproject.toml`：https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- `pydantic-settings` 文档（`.env` 读取与校验器）：https://docs.pydantic.dev/latest/concepts/pydantic_settings/

---

## 修订记录

- 2026-11-17 初稿（会话内版本，未落盘）
- 2026-11-17 落地为文件；按实机输出修正 §3 版本表、§6 报错原文、§7 全部输出；移除本节未使用的 `structlog` 依赖，改由 1.5 追加；补充 §5.5 `extra="ignore"` 实测坑与 §8 坑 1（Windows Store 别名）
