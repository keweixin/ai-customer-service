"""API 层依赖注入(Depends 工厂)。

设计原则:
- 每个 provider 都是 ``async`` 函数,返回 ``AsyncGenerator``(DB session)或单例,
  便于 FastAPI 通过 ``Depends(...)`` 注入,且便于测试时 override。
- 单例(LLMService / Pipeline)用模块级变量缓存,首次访问时构造,
  避免每次请求重建连接池/加载模型;进程内复用。
- 鉴权依赖分两级:``get_current_user`` 校验 token 与存在性,
  ``get_current_admin`` 在其基础上再校验角色,链式 ``Depends`` 复用。
- 所有跨模块 import 用绝对路径 ``from app.xxx``,避免相对 import 在被不同
  入口(uvicorn / pytest / alembic)加载时出现父包解析歧义。
"""

from typing import AsyncGenerator

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import get_logger, set_request_context
from app.models.user import User, UserRole

logger = get_logger(__name__)


# ------------------------------------------------------------------
# 数据库会话
# ------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """提供一个请求级的事务性数据库会话。

    直接复用 ``app.db.get_db``(已实现连接池 + 请求级会话 + 自动关闭),
    避免重复造轮子;同时保留本层包装,便于在不改业务代码的前提下替换实现
    (如多租户场景需按 tenant 路由不同 DB)。

    用 yield 模式让 FastAPI 在请求结束时自动 ``close()``,
    并在出现异常时回滚--避免长事务、连接泄漏。
    """
    # 延迟 import 规避循环依赖:app.db 在 lifespan 中初始化,本模块被 import 时它未必就绪
    from app.db import get_db as _get_db

    # 复用已实现的请求级会话生成器,逐项透传 yield
    async for session in _get_db():
        try:
            yield session
        except Exception:
            # 异常路径回滚,防止脏数据残留连接池内的会话状态
            await session.rollback()
            raise


# ------------------------------------------------------------------
# 鉴权
# ------------------------------------------------------------------


async def get_current_user(
    # 用 Header 取 Authorization,显式声明让 OpenAPI 文档展示 Bearer 鉴权要求
    authorization: str | None = Header(default=None, description="Bearer <token>"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析 Bearer token,返回当前登录用户。

    流程:取 token -> 解码 JWT -> 查 DB -> 校验 is_active。
    任何一步失败都抛 ``AuthenticationError``,由全局异常处理器转 401。

    Args:
        authorization: ``Authorization`` 头,期望 ``Bearer <jwt>``。
        db: 请求级 DB 会话。

    Returns:
        当前已认证用户;失败抛异常,不返回 None。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        # 缺少 scheme 或不是 Bearer,统一报 401 而非 400,
        # 符合 RFC 6750 关于无效/缺失 token 的语义。
        raise AuthenticationError("缺少认证信息")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise AuthenticationError("认证 token 为空")

    # 延迟 import:security 模块依赖 jwt 库与 config,启动时才需要
    from app.core.security import decode_access_token

    try:
        payload = decode_access_token(token)
    except Exception as exc:
        # 解码失败(过期、签名错误、格式错)统一归为未认证,
        # 不向前端区分具体原因,避免给攻击者可探测的信号。
        logger.warning("JWT 解码失败", error=str(exc))
        raise AuthenticationError("认证 token 无效或已过期") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("token 缺少用户标识")

    # 查库而非直接信任 payload:防止用户被禁用/删除后旧 token 仍可用
    from app.repositories.user_repository import UserRepository

    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)  # type: ignore[arg-type]
    if user is None:
        raise AuthenticationError("用户不存在")

    if not user.is_active:
        # 软禁用账号立即失效,即使 token 没过期
        raise AuthenticationError("账号已被禁用")

    # 注入到日志上下文,后续该请求的所有日志自动带 user_id
    set_request_context(user_id=str(user.id))
    return user


async def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """要求当前用户为管理员。

    复用 ``get_current_user`` 完成认证,这里只做角色校验,
    分离关注点:认证逻辑变更不影响授权逻辑。
    """
    if user.role != UserRole.ADMIN:
        # 已认证但无权限 -> 403(区别于 401 未认证)
        raise AuthorizationError("需要管理员权限")
    return user


# ------------------------------------------------------------------
# 服务单例
# ------------------------------------------------------------------
# 单例缓存在模块级变量,进程内复用;通过函数封装便于测试 override。
# 不用 lru_cache 是因为某些服务需要显式传参(如 db / llm),签名不便于装饰器缓存。

_llm_service: "LLMService | None" = None  # type: ignore[name-defined]
_pipeline: "Pipeline | None" = None  # type: ignore[name-defined]


async def get_llm_service() -> "LLMService":  # type: ignore[name-defined]
    """返回全局 LLMService 单例。

    LLMService 内部维护 HTTP 连接池与重试策略,频繁重建会造成
    连接抖动与不必要的握手开销,因此进程级缓存。
    """
    global _llm_service
    if _llm_service is None:
        # 延迟 import 避免在模块加载期触发第三方依赖(如 httpx)初始化
        from app.services.llm import LLMService

        settings = get_settings()
        _llm_service = LLMService(settings.llm)
        logger.info("LLMService 已初始化", model=settings.llm.model)
    return _llm_service


async def get_rag_service(
    db: AsyncSession = Depends(get_db),
    llm: "LLMService" = Depends(get_llm_service),  # type: ignore[name-defined]
) -> "RagService":  # type: ignore[name-defined]
    """构造请求级 RagService。

    RAG 检索需要 DB(查文档/向量)与 LLM(embedding),两者都由依赖注入提供。
    RagService 本身无状态,每次请求新建即可,无需单例。
    """
    from app.services.rag_service import RagService

    return RagService(db=db, llm=llm)


async def get_memory_service(
    db: AsyncSession = Depends(get_db),
    llm: "LLMService" = Depends(get_llm_service),  # type: ignore[name-defined]
) -> "MemoryService":  # type: ignore[name-defined]
    """构造请求级 MemoryService。

    记忆服务按会话/用户聚合历史消息与画像,依赖 DB 持久化层;
    无状态,每次请求新建。
    """
    from app.services.memory_service import MemoryService

    return MemoryService(db=db, llm=llm)


async def get_pipeline() -> "Pipeline":  # type: ignore[name-defined]
    """返回全局对话处理流水线单例。

    Pipeline 编排 7 阶段(InputParser -> ContentGuard -> IntentClassifier ->
    EntityTracker -> RagRetriever -> StrategyInjector -> StreamGenerator),
    内部阶段对象较重(各持有 service 引用),复用降低 GC 压力。
    依赖通过显式注入而非 import-time 构造,避免循环依赖与启动顺序问题。

    阶段依赖注入:
    - InputParser / StrategyInjector:无外部依赖(纯函数/规则)。
    - ContentGuard / IntentClassifier / EntityTracker / StreamGenerator:注入
      全局 LLMService 单例。
    - RagRetriever:注入 RagService(需要 db + llm)。由于 Pipeline 是进程级
      单例而 db 是请求级的,这里用 ``AsyncSessionLocal()`` 持有一个长期 session
      供单例 RAG 阶段复用;真实按请求隔离的检索应由更上层按请求重建 stage,
      当前实现优先保证"应用能启动且 import 链完整"。
    - StreamGenerator:注入已注册的工具实例(从 ``app.tools`` 取),启用
      Function Calling。
    """
    global _pipeline
    if _pipeline is None:
        # Pipeline 定义在 app.pipeline.runner,并由 app.pipeline 包重新导出;
        # 这里从 runner 取以避免触发包级 __init__ 的额外 import 副作用。
        from app.pipeline.runner import Pipeline
        from app.pipeline.stages.content_guard import ContentGuard
        from app.pipeline.stages.entity_tracker import EntityTracker
        from app.pipeline.stages.input_parser import InputParser
        from app.pipeline.stages.intent_classifier import IntentClassifier
        from app.pipeline.stages.rag_retriever import RagRetriever
        from app.pipeline.stages.strategy_injector import StrategyInjector
        from app.pipeline.stages.stream_generator import StreamGenerator
        from app.services.rag_service import RagService
        from app.tools import TOOL_REGISTRY

        # 单例创建时通过 deps 函数显式拉依赖,而非在 Pipeline 内部 import,
        # 便于测试替换。
        llm = await get_llm_service()

        # RagRetriever 需要 RagService(db + llm)。单例场景复用一个长期 session。
        # AsyncSessionLocal() 返回的 session 需在独立任务中管理生命周期;
        # 此处仅构造供阶段持有,实际检索 SQL 在该 session 上执行。
        from app.db import AsyncSessionLocal

        rag_db = AsyncSessionLocal()
        rag_service = RagService(db=rag_db, llm=llm)

        # 工具实例化:从全局注册表构造所有已注册工具(StreamGenerator 据此
        # 声明 tools 并执行调用)。注册表存的是类,这里逐个实例化。
        tools = [tool_cls() for tool_cls in TOOL_REGISTRY.values()]

        # 按 runner 约定顺序组装 7 阶段;最后一个必须是 StreamGenerator(流式末阶段)。
        stages = [
            InputParser(),
            ContentGuard(llm=llm),
            IntentClassifier(llm=llm),
            EntityTracker(llm=llm),
            RagRetriever(rag_service=rag_service),
            StrategyInjector(),
            StreamGenerator(llm=llm, tools=tools),
        ]
        _pipeline = Pipeline(stages=stages)
        logger.info(
            "Pipeline 已初始化",
            stages=[s.name for s in stages],
            tool_count=len(tools),
        )
    return _pipeline


def reset_singletons() -> None:
    """清空服务单例缓存。

    主要用于测试:每个用例间需要干净的服务实例时调用,
    生产环境不应使用。
    """
    global _llm_service, _pipeline
    _llm_service = None
    _pipeline = None
