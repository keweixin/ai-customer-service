"""结构化日志模块(基于 structlog)。

特性:
- 生产环境输出 JSON(便于 ELK/Loki 采集),开发环境彩色 console。
- 通过 contextvars 注入 request_id / user_id / session_id,
  同一请求内所有日志自动带上这些字段,无需手动传递。
- ``get_logger()`` 返回已绑定上下文的 logger,业务代码直接调用即可。
- 日志级别从 config 读取,统一在 ``configure_logging()`` 一次性初始化。
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

# ------------------------------------------------------------------
# ContextVars:跨异步任务透传的上下文字段
# 使用 ContextVar 而非线程局部变量,是因为 FastAPI/asyncio 下
# 一个请求的生命周期跨多个协程,threadlocal 无法正确隔离。
# ------------------------------------------------------------------
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")


def set_request_context(
    *, request_id: str | None = None, user_id: str | None = None, session_id: str | None = None
) -> None:
    """在请求中间件中调用,设置当前请求的上下文字段。

    设置后在同一次请求(及其派生的协程)中,所有 structlog 日志
    会自动带上这些字段。
    """
    if request_id is not None:
        request_id_var.set(request_id)
    if user_id is not None:
        user_id_var.set(user_id)
    if session_id is not None:
        session_id_var.set(session_id)


def clear_request_context() -> None:
    """请求结束时清理上下文,防止 contextvar 在协程复用时串数据。"""
    request_id_var.set("")
    user_id_var.set("")
    session_id_var.set("")


def _inject_contextvars(
    _logger: Any, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor:把 ContextVars 注入每条日志事件。

    仅在字段非空时注入,避免日志里出现大量空字符串。
    """
    rid = request_id_var.get()
    uid = user_id_var.get()
    sid = session_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    if uid:
        event_dict["user_id"] = uid
    if sid:
        event_dict["session_id"] = sid
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """初始化 structlog 与标准 logging,应在应用启动时调用一次。

    Args:
        level: 日志级别字符串(DEBUG/INFO/WARNING/ERROR)。
        json_output: True 用 JSON renderer(生产),False 用 console 彩色渲染(开发)。
    """
    # 标准 logging 也需要配置 level,structlog 的 stdlib 兼容层会读取它
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # 共享 processor 链:contextvars -> 时间戳 -> 日志级别 -> 事件
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_contextvars,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """返回带上下文的 structlog logger。

    Args:
        name: 通常传 ``__name__``,作为 ``logger`` 字段记录调用源。

    Returns:
        已配置的 BoundLogger,业务侧直接 ``logger.info("...", key=value)`` 即可。
    """
    return structlog.get_logger(name)


def init_from_config() -> None:
    """从应用配置读取日志级别与环境,完成初始化。

    封装为函数避免业务代码直接依赖 Settings,降低耦合。
    """
    # 延迟 import 避免循环依赖:config 不依赖 logging
    from app.config import get_settings

    settings = get_settings()
    configure_logging(
        level=settings.app.log_level,
        # 生产用 JSON,其他环境用彩色 console 便于本地调试
        json_output=settings.app.is_production,
    )
