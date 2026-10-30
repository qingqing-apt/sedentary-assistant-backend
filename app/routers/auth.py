from fastapi import APIRouter

from ..deps import PREFIX
from ..errors import AppError
from ..schemas import LoginIn, LoginOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut, summary="练习用登录")
def login(payload: LoginIn) -> LoginOut:
    name = (payload.username or "").strip()
    if not name:
        raise AppError(400, "EMPTY_USERNAME", "请输入用户名", "username")
    if len(name) > 32:
        raise AppError(400, "USERNAME_TOO_LONG", "用户名太长了", "username")
    return LoginOut(token=f"{PREFIX}{name}", user_id=name)
