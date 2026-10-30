"""Agent 端点的登录校验与用户身份隔离。

两条硬规则，写死的：

1. **未登录在跑模型之前就被拒**。不能等 Agent 开始思考、调了工具才发现没身份。
2. **工具拿不到 user_id 参数**。身份只从后端解析出的 ContextVar 读取，模型和浏览器
   都无法指定"我要查谁的数据"。

这和 REST 接口的 `deps.current_user` 是同一套凭证格式（`Bearer demo.<用户名>`），
换成真实 JWT 时只需要替换这一个文件。
"""

from __future__ import annotations

import contextvars

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..deps import PREFIX

# 当前请求的已验证用户（工具通过它取数据，永不来自模型输入）
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_current_user_id", default=None
)

_bearer = HTTPBearer(auto_error=False)


def get_current_user_id() -> str | None:
    """返回当前请求已验证的用户名（供 Agent 工具使用）。"""
    return _current_user_id.get()


async def resolve_authorization_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """AG-UI 入口的授权解析器：校验凭证并返回按用户隔离的 scope。

    该函数是 FastAPI 依赖，由 Agent Framework 的 AG-UI 端点调用。
    返回的 scope 是服务端命名空间，用于隔离不同用户的会话 / thread 状态。
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    if not token.startswith(PREFIX) or len(token) <= len(PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    username = token[len(PREFIX) :]
    # 把已验证身份注入上下文，供工具使用（不是来自模型，也不是浏览器参数）
    _current_user_id.set(username)
    return f"user:{username}"
