"""统一的业务异常与错误响应体。

所有接口失败都返回 {code, message, field?}，前端按 code 分支处理，
不靠解析自然语言文案。
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field


def _body(code: str, message: str, field: str | None = None) -> dict:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.field),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return JSONResponse(
            status_code=400,
            content=_body("INVALID_PAYLOAD", first.get("msg", "请求参数不合法"), field or None),
        )
