"""自定义异常层级与 FastAPI 异常处理器。

设计目标:
1. 业务代码抛领域异常(如 ``LLMError``),不直接返回 HTTP 响应,
   关注点分离让 service 层保持纯粹。
2. 异常带 ``error_code``(字符串如 ``LLM_001``),前端可据此做精确提示,
   与 HTTP 状态码解耦——同一 422 可能对应多种业务错误。
3. 通过 ``register_exception_handlers`` 一次性把所有异常映射成统一 JSON 响应:
   ``{"error": {"code": "...", "message": "..."}}``
4. 兜底:未捕获的 ``Exception`` → 500 InternalError,避免堆栈外泄给客户端。
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError as FastAPIRequestValidationError

# 统一响应体结构,前端按此契约解析错误
ERROR_RESPONSE_KEY: Final[str] = "error"


class BaseAppError(Exception):
    """所有应用自定义异常的基类。

    Attributes:
        error_code: 业务错误码(字符串),如 ``LLM_001``,供前端精确分支处理。
        message: 面向用户的可读错误信息(避免泄露内部实现细节)。
        status_code: 映射的 HTTP 状态码。
        detail: 附加调试信息,仅在非生产环境返回给客户端。
    """

    error_code: str = "APP_000"
    message: str = "应用内部错误"
    status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        # 允许子类实例化时覆盖默认值,提升复用性
        self.message = message or self.__class__.message
        if error_code is not None:
            self.error_code = error_code
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail or {}
        super().__init__(self.message)

    def to_dict(self, include_detail: bool = False) -> dict[str, object]:
        """序列化为响应字典。

        Args:
            include_detail: 是否包含 detail 调试信息(仅非生产环境开启)。
        """
        payload: dict[str, object] = {
            "code": self.error_code,
            "message": self.message,
        }
        if include_detail and self.detail:
            payload["detail"] = self.detail
        return payload


# ------------------------------------------------------------------
# 业务异常子类:每个对应一个错误码前缀,便于按域排查
# ------------------------------------------------------------------


class ValidationError(BaseAppError):
    """输入参数校验失败。"""

    error_code = "VAL_001"
    message = "请求参数校验失败"
    status_code = 422


class AuthenticationError(BaseAppError):
    """未认证(token 缺失/无效/过期)。"""

    error_code = "AUTH_001"
    message = "认证失败,请重新登录"
    status_code = 401


class AuthorizationError(BaseAppError):
    """已认证但无权限访问该资源。"""

    error_code = "AUTHZ_001"
    message = "无权访问该资源"
    status_code = 403


class NotFoundError(BaseAppError):
    """资源不存在。"""

    error_code = "NF_001"
    message = "资源不存在"
    status_code = 404


class LLMError(BaseAppError):
    """LLM 调用相关错误(超时、限流、上游 5xx 等)。"""

    error_code = "LLM_001"
    message = "AI 服务暂时不可用,请稍后重试"
    status_code = 502


class DatabaseError(BaseAppError):
    """数据库操作错误。"""

    error_code = "DB_001"
    message = "数据服务异常"
    status_code = 500


class RateLimitError(BaseAppError):
    """触发限流。"""

    error_code = "RL_001"
    message = "请求过于频繁,请稍后再试"
    status_code = 429


class RAGError(BaseAppError):
    """RAG 检索/生成相关错误。"""

    error_code = "RAG_001"
    message = "知识库检索失败"
    status_code = 500


class PipelineError(BaseAppError):
    """对话处理流水线错误(编排/路由阶段)。"""

    error_code = "PIPE_001"
    message = "对话处理失败"
    status_code = 500


class InternalError(BaseAppError):
    """未分类的内部错误,作为全局兜底。"""

    error_code = "INT_001"
    message = "服务器内部错误"
    status_code = 500


# ------------------------------------------------------------------
# FastAPI 异常处理器注册
# ------------------------------------------------------------------


def _error_response(
    status_code: int,
    code: str,
    message: str,
    detail: dict[str, object] | None = None,
) -> JSONResponse:
    """构造统一错误 JSON 响应。"""
    body: dict[str, object] = {
        ERROR_RESPONSE_KEY: {"code": code, "message": message}
    }
    if detail:
        body[ERROR_RESPONSE_KEY]["detail"] = detail  # type: ignore[assignment]
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI, *, include_detail: bool = False) -> None:
    """向 FastAPI 应用注册全部异常处理器。

    Args:
        app: FastAPI 实例。
        include_detail: 是否在响应里返回 detail 调试信息,
            生产环境应设为 False 防止泄露内部状态。
    """

    @app.exception_handler(BaseAppError)
    async def _handle_app_error(_: Request, exc: BaseAppError) -> JSONResponse:
        # 所有自定义异常走统一出口,状态码由异常自身决定
        return _error_response(
            exc.status_code,
            exc.error_code,
            exc.message,
            detail=exc.detail if include_detail else None,
        )

    @app.exception_handler(FastAPIRequestValidationError)
    async def _handle_fastapi_validation(
        _: Request, exc: FastAPIRequestValidationError
    ) -> JSONResponse:
        # FastAPI 内置参数校验错误转换为统一格式,error_code 固定 VAL_002
        return _error_response(
            422,
            "VAL_002",
            "请求参数校验失败",
            detail={"errors": exc.errors()} if include_detail else None,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 全局兜底:任何未捕获异常统一转为 500,避免堆栈泄露给客户端。
        # 这里 import 在函数内是为了避免循环依赖,structlog 配置完成后才可用。
        from app.core.logging import get_logger

        logger = get_logger(__name__)
        # 记录完整堆栈到日志,但响应里只给通用提示
        logger.exception("未捕获异常", error_type=type(exc).__name__)
        return _error_response(
            500,
            InternalError.error_code,
            InternalError.message,
        )
