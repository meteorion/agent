# file: src/servicedesk/mcp/auth.py
"""MCP 的鉴权：Bearer token + scope 校验。

核心是把两种失败分清楚：

    缺参数  → HTTP 200 + resultType=input_required   「问用户就能解决」
    缺权限  → HTTP 403 + insufficient_scope          「问用户没用，要换身份或提权」

混在一起，用户会被告知"请补充信息"，而他补一辈子也补不出一个 scope。

⚠️ 这里用不透明 token + 内存存储做演示。生产里应校验 JWT 的 `iss`（RFC 9207）
并与授权服务器的 JWKS 对齐；DCR 已被 CIMD 取代（见 2.3 的变更表）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# 语义化 scope：读、写、审批
SCOPE_READ = "tools:read"
SCOPE_WRITE = "tools:write"
SCOPE_APPROVE = "admin:approve"

ALL_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_APPROVE})

# 每个工具需要哪些 scope
TOOL_SCOPES: dict[str, frozenset[str]] = {
    "query_asset": frozenset({SCOPE_READ}),
    "query_permission": frozenset({SCOPE_READ}),
    "query_ticket": frozenset({SCOPE_READ}),
    "create_ticket": frozenset({SCOPE_WRITE}),
}


# ------------------------------- 错误 -------------------------------


class AuthError(Exception):
    """401：没带 token、token 无效或过期。"""

    status = 401
    error_code = "invalid_token"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code

    @property
    def www_authenticate(self) -> str:
        """注意：HTTP 头只能是 latin-1/ASCII。

        把中文错误描述塞进 `error_description` 会让 `send_header` 抛
        `UnicodeEncodeError`，直接 500 —— 人话放响应体，头里只放机器可读的码。
        """
        return f'Bearer realm="servicedesk-mcp", error="{self.error_code}"'


class InsufficientScope(AuthError):
    """403：token 有效，但权限不够。

    **不要把这条降级成 401**：401 会诱导客户端去"重新登录"，
    而重新登录也拿不到缺少的 scope——它需要提权审批。
    """

    status = 403
    error_code = "insufficient_scope"

    def __init__(self, tool: str, required: frozenset[str], granted: frozenset[str]) -> None:
        self.tool = tool
        self.required = required
        self.granted = granted
        missing = sorted(required - granted)
        super().__init__(
            f"工具 {tool} 需要 {missing}，当前 token 只有 {sorted(granted)}",
            error_code="insufficient_scope",
        )

    @property
    def www_authenticate(self) -> str:
        return (
            f'Bearer realm="servicedesk-mcp", error="insufficient_scope", '
            f'scope="{" ".join(sorted(self.required))}"'
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "error": "insufficient_scope",
            "tool": self.tool,
            "required": sorted(self.required),
            "granted": sorted(self.granted),
        }


# ------------------------------- 令牌 -------------------------------


@dataclass(frozen=True)
class Claims:
    subject: str
    scopes: frozenset[str]
    expires_at: float

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()


@dataclass
class TokenStore:
    """演示用：不透明 token → claims。生产里换成 JWT + JWKS 校验。"""

    _tokens: dict[str, Claims] = field(default_factory=dict)

    def issue(self, subject: str, scopes: set[str] | frozenset[str], *, ttl_s: float = 3600) -> str:
        unknown = set(scopes) - ALL_SCOPES
        if unknown:
            raise ValueError(f"未知 scope: {sorted(unknown)}")
        token = f"tok_{secrets.token_urlsafe(24)}"
        self._tokens[token] = Claims(
            subject=subject,
            scopes=frozenset(scopes),
            expires_at=time.time() + ttl_s,
        )
        return token

    def verify(self, token: str) -> Claims:
        claims = self._tokens.get(token)
        if claims is None:
            raise AuthError("token 无效")
        if claims.expired:
            self._tokens.pop(token, None)
            raise AuthError("token 已过期", error_code="expired_token")
        return claims


def parse_bearer(header: str | None) -> str:
    """从 `Authorization: Bearer xxx` 里取出 token。"""
    if not header:
        raise AuthError("缺少 Authorization 头", error_code="missing_token")
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("Authorization 格式应为 `Bearer <token>`", error_code="invalid_request")
    return parts[1].strip()


def dynamic_scopes(tool: str, arguments: dict[str, object] | None) -> frozenset[str]:
    """按参数追加 scope 要求。

    静态表（`TOOL_SCOPES`）只能表达"这个工具需要什么"，
    但真实业务里常常是"这个工具在**某些参数下**才需要更高权限"——
    比如建 P0 工单必须有人有审批权，建 P3 不用。
    """
    if tool == "create_ticket" and (arguments or {}).get("priority") == "P0":
        return frozenset({SCOPE_APPROVE})
    return frozenset()


def check_scope(
    claims: Claims, tool: str, arguments: dict[str, object] | None = None
) -> None:
    """按工具（与参数）查所需 scope。不够就抛 InsufficientScope。"""
    required = TOOL_SCOPES.get(tool, frozenset()) | dynamic_scopes(tool, arguments)
    if not required.issubset(claims.scopes):
        raise InsufficientScope(tool, required, claims.scopes)
