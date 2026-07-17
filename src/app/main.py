"""FastAPI 应用入口。

职责:
- 构造 FastAPI 实例(含 title/version/description,供 OpenAPI 文档展示);
- lifespan:启动时初始化日志、建表(开发期)、关闭时释放连接池;
- 注册中间件(request_id / 日志 / CORS / 限流);
- 注册全局异常处理器(从 core.exceptions 导入);
- 挂载 api_router 到 ``/api/v1``;
- 暴露运维端点:/health、/ready、/metrics(prometheus)。

为什么把这些集中在 main.py:lifespan 与中间件注册顺序对应用行为影响大,
集中管理便于审计与排查启动期问题。具体实现下沉到各 core/ 子模块,
main 只做编排。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.logging import get_logger, init_from_config
from app.core.middleware import setup_middleware
from app.core.rate_limit import limiter

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:启动前初始化,关闭时清理。

    启动顺序:配置校验 -> 日志 -> DB 建表(开发期) -> 单例预热(可选)。
    关闭顺序:释放 DB 连接池。任何步骤失败都应尽早暴露,而非运行时才报错。
    """
    settings = get_settings()

    # 1. 生产环境硬性校验:密钥不得为默认值,提前失败优于运行时泄露
    settings.validate_production()

    # 2. 日志初始化:必须在打任何业务日志之前,确保 renderer 就绪
    init_from_config()
    logger.info(
        "应用启动中",
        env=settings.app.env,
        name=settings.app.name,
        port=settings.app.port,
    )

    # 3. DB 初始化:开发期用 init_db 建表 + 启用 pgvector;
    #    生产用 Alembic 迁移,这里仍跑 init_db 但仅幂等启用扩展,不破坏已有表。
    from app.db import engine, init_db

    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as exc:  # noqa: BLE001
        # 建表失败不阻止启动--可能是迁移已由 Alembic 管理或 DB 暂不可达。
        # /ready 端点会反映真实连通性,这里只记日志。
        logger.error("数据库初始化失败(已忽略,由 /ready 反映状态)", error=str(exc))

    yield  # 应用运行期

    # 4. 关闭:释放连接池,避免连接泄漏导致重启时端口/连接耗尽
    logger.info("应用关闭中,释放数据库连接池")
    await engine.dispose()
    logger.info("应用已停止")


def create_app() -> FastAPI:
    """构造 FastAPI 应用实例(工厂模式)。

    用工厂而非模块级全局 app,便于:
    - 测试时按需构造不同配置的 app;
    - 多 worker 下各进程独立初始化;
    - 避免 import 副作用(模块 import 即启动中间件注册)。
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app.name,
        version="1.0.0",
        description=(
            "AI 客服系统对外 API。提供鉴权、流式对话、知识库管理、"
            "管理后台等能力。流式对话走 SSE,知识库管理需管理员权限。"
        ),
        # 生产环境关闭 docs 减少攻击面;开发/预发保留便于联调
        docs_url="/docs" if not settings.app.is_production else None,
        redoc_url="/redoc" if not settings.app.is_production else None,
        openapi_url="/openapi.json" if not settings.app.is_production else None,
        lifespan=lifespan,
    )

    # 中间件注册(顺序见 core.middleware.setup_middleware 文档)
    setup_middleware(app)
    # 把 limiter 绑定到 app.state,供路由装饰器与 SlowAPIMiddleware 共享
    # (setup_middleware 内部已设,这里显式再赋一次便于阅读与测试断言)
    app.state.limiter = limiter

    # 全局异常处理器:生产环境不返回 detail,避免泄露内部状态
    from app.core.exceptions import register_exception_handlers

    register_exception_handlers(app, include_detail=not settings.app.is_production)

    # 业务路由聚合:/api/v1 前缀由 api_router 内部声明
    from app.api.v1 import api_router

    app.include_router(api_router)

    # ---- 运维端点 ----
    # 健康检查:轻量,只表示进程存活,适合 K8s liveness probe
    @app.get(
        "/health",
        tags=["ops"],
        summary="健康检查(liveness)",
    )
    async def health() -> JSONResponse:
        """liveness 探针:进程存活即返回 ok,不检查依赖。"""
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "version": "1.0.0"},
        )

    # 就绪检查:检查 DB/LLM 连通性,适合 K8s readiness probe
    @app.get(
        "/ready",
        tags=["ops"],
        summary="就绪检查(readiness)",
    )
    async def ready() -> JSONResponse:
        """readiness 探针:依赖(DB/LLM)可用才返回 200,否则 503。"""
        checks: dict[str, str] = {}
        all_ok = True

        # DB 连通性:发一条极轻量 SQL
        try:
            from sqlalchemy import text

            from app.db import engine

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"fail: {type(exc).__name__}"
            all_ok = False

        # LLM 连通性:不实际发请求(有成本),只校验配置完整
        # 真实探测放在定时任务或专用探针,避免 readiness 频繁调用产生费用
        try:
            llm_cfg = settings.llm
            if llm_cfg.api_key.get_secret_value() and llm_cfg.model:
                checks["llm"] = "ok"
            else:
                checks["llm"] = "fail: 未配置 API key 或模型"
                all_ok = False
        except Exception as exc:  # noqa: BLE001
            checks["llm"] = f"fail: {type(exc).__name__}"
            all_ok = False

        return JSONResponse(
            status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if all_ok else "not_ready", "checks": checks},
        )

    # Prometheus 指标:prometheus_client 提供 make_asgi_app,挂到 /metrics
    # 延迟 import 避免无监控需求的部署也强依赖该库--但 .env 未排除,这里默认启用
    try:
        from prometheus_client import make_asgi_app

        app.mount("/metrics", make_asgi_app())
    except ImportError:
        # prometheus_client 未安装时跳过,不影响主流程
        logger.warning("prometheus_client 未安装,/metrics 端点不可用")

    return app


# 模块级 app:uvicorn 直接引用 ``app.main:app`` 启动。
# 用工厂创建保证 lifespan/中间件已就绪。
app = create_app()
