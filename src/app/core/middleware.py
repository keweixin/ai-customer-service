"""中间件模块。

包含:
- ``RequestIdMiddleware``:为每个请求生成唯一 request_id,注入 contextvar 与响应头。
- ``LoggingMiddleware``:记录请求方法/路径/状态码/耗时。
- ``setup_middleware``:一次性挂载上述中间件 + CORS + slowapi 限流。

设计要点:中间件顺序敏感。FastAPI/Starlette 中后添加的中间件先执行(洋葱模型),
因此把最外层(日志、request_id)放在最后添加,确保它们包裹业务处理。
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.core.logging import (
    clear_request_context,
    get_logger,
    set_request_context,
)

# 响应头名称常量,客户端据此关联日志/链路追踪
REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成唯一 request_id 并注入上下文与响应头。

    若客户端已带 ``X-Request-ID`` 则透传(便于跨服务链路追踪),
    否则用 uuid4 生成。请求结束时清理 contextvar 防止协程复用串数据。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        set_request_context(request_id=rid)

        try:
            response: Response = await call_next(request)
        finally:
            # 无论请求成功与否都要清理,防止 contextvar 泄漏到下一个请求
            clear_request_context()

        response.headers[REQUEST_ID_HEADER] = rid
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """记录每个 HTTP 请求的方法/路径/状态码/耗时。

    在请求开始与结束各打一条日志,便于追踪慢请求与异常。
    耗时统计用 perf_counter 而非 time.time(),精度更高。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        logger = get_logger(__name__)
        start = time.perf_counter()

        logger.info(
            "request.start",
            method=request.method,
            path=request.url.path,
        )

        try:
            response: Response = await call_next(request)
        except Exception:
            # 业务异常会被 exception_handler 处理,这里仅记录未处理情况
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "request.error",
                method=request.method,
                path=request.url.path,
                duration_ms=round(elapsed, 2),
            )
            raise

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "request.end",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(elapsed, 2),
        )
        return response


def create_limiter() -> Limiter:
    """根据配置创建 slowapi Limiter 实例(工厂,保留供测试自定义用)。

    使用客户端真实 IP 作为限流 key,通过 ``get_remote_address`` 获取,
    已正确处理 X-Forwarded-For。
    """
    settings = get_settings()
    return Limiter(
        key_func=get_remote_address,
        default_limits=[settings.rate_limit.limiter_limit],
    )


def setup_middleware(app: FastAPI) -> Limiter:
    """一次性挂载全部中间件,返回挂载到 app.state 的 Limiter。

    关键:s здесь 复用 ``app.core.rate_limit.limiter`` 全局单例,而非新建。
    原因是路由装饰器 ``@limiter.limit(...)``(见 v1/auth.py)在模块加载期
    就绑定了某个 Limiter 实例;SlowAPIMiddleware 执行限流时读取
    ``app.state.limiter``。两者必须是同一对象,否则装饰器声明的限制不会生效。
    因此以模块级单例为准,``setup_middleware`` 只负责把它挂到 app.state。

    挂载顺序说明(Starlette 洋葱模型,最后添加的最先执行):
      1. SlowAPIMiddleware   -- 最内层,路由匹配后再限流
      2. CORSMiddleware
      3. LoggingMiddleware   -- 包裹业务,记录完整耗时
      4. RequestIdMiddleware -- 最外层,确保所有日志都有 request_id
    """
    settings = get_settings()
    # 复用全局单例,确保装饰器与中间件引用同一 Limiter 实例
    from app.core.rate_limit import limiter

    # 1. CORS:必须在路由之前挂载,preflight 请求才不会被业务拦截
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    # 2. 限流:slowapi 需先设置 app.state.limiter 与异常处理器,再加 middleware
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # 3 & 4. 日志与 request_id:放最后添加,执行顺序最外层
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    return limiter
