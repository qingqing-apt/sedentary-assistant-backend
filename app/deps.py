"""鉴权依赖。

练习环境用一个极简 token（`demo.<用户名>`）代替真实 JWT。
接入第二天的登录实现时，只需要替换这个函数，其它代码不用动。
"""

from fastapi import Header

from .errors import AppError

PREFIX = "demo."


def current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "UNAUTHORIZED", "请先登录")

    token = authorization[7:].strip()
    if not token.startswith(PREFIX) or len(token) <= len(PREFIX):
        raise AppError(401, "UNAUTHORIZED", "登录信息已失效，请重新登录")

    return token[len(PREFIX):]
