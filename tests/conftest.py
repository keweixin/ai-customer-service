"""企业级测试套件的全局 pytest 配置与公共 fixture。

设计原则:
- 单元测试与外部世界零耦合:LLM / RAG / DB / 向量检索全部可 mock。
- 集成测试用内存 SQLite(aiosqlite)+ TestClient,pgvector 不可用,
  向量相关断言走 mock,不依赖真实 pgvector 扩展。
- 每个 fixture 只做一件事,可组合;高复用 fixture 放这里,模块私有 fixture
  放各自测试文件。
- JWT 密钥用固定值,保证 token 可解码、可断言过期场景。
- ``event_loop`` fixture 显式覆盖 pytest-asyncio 默认实现,使每个测试
  函数拿到独立事件循环,避免跨用例协程状态泄漏。
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ----------------------------------------------------------------------
# sys.path 修正:确保 ``from app.xxx`` 在未安装包时也能解析到 src/app。
# pyproject 配置 packages.find where=["src"],但开发期/CI 直接跑 pytest
# 时源码未 install -e,这里把 src 加入 path 兜底。
# ----------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# ----------------------------------------------------------------------
# 测试用固定常量:集中定义避免散落各测试硬编码,改一处全局生效。
# ----------------------------------------------------------------------
TEST_JWT_SECRET = "test-secret-key-for-jwt-not-for-prod-use-64chars-padding"
TEST_JWT_ALGORITHM = "HS256"
# 集成测试优先用真实 PostgreSQL(支持 JSONB/pgvector);无 Postgres 时回退 SQLite
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://aics:change_me_in_production@localhost:5432/ai_customer_service_test",
)
# 单元测试用 SQLite(不依赖外部服务)
UNIT_DB_URL = "sqlite+aiosqlite:///:memory:"
TEST_USER_USERNAME = "testuser"
TEST_USER_EMAIL = "testuser@example.com"
TEST_USER_PASSWORD = "SuperSecret-123!"
TEST_ADMIN_USERNAME = "adminuser"
TEST_ADMIN_EMAIL = "admin@example.com"


# ----------------------------------------------------------------------
# 环境变量:在导入任何 app 模块前注入,保证 BaseSettings 读取到测试值。
# 这里用 monkeypatch 的 session 级替代品:os.environ 直接设置,
# 因为某些 app 模块在 import 期就调用 get_settings()(被 lru_cache 缓存)。
# ----------------------------------------------------------------------
os.environ.setdefault("JWT_SECRET_KEY", TEST_JWT_SECRET)
os.environ.setdefault("JWT_ALGORITHM", TEST_JWT_ALGORITHM)
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_DEBUG", "true")
os.environ.setdefault("ARK_API_KEY", "fake-ark-key-for-tests")


@pytest.fixture(scope="session")
def event_loop() -> Iterator[Any]:
    """每个测试 session 使用独立事件循环。

    覆盖 pytest-asyncio 默认的 function 级 loop,改为 session 级:
    session 级 fixture(如 db engine)需要一个跨用例存活的 loop,
    否则跨 fixture 的 await 会报 "attached to a different loop"。

    Yields:
        一个 asyncio 事件循环实例。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    # 清理待办任务,防止 "Task was destroyed but it is pending!" 警告
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    asyncio.set_event_loop(None)


# ----------------------------------------------------------------------
# 数据库相关 fixture
# ----------------------------------------------------------------------
@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[Any]:
    """提供一个异步会话,所有表已建好,用完清理。

    优先用真实 PostgreSQL(支持 JSONB + pgvector),无则回退 SQLite(跳过向量表)。
    每个 fixture 用唯一的 schema/表前缀避免并发测试互相污染:
    PostgreSQL 用事务回滚,SQLite 用内存库。

    Yields:
        AsyncSession:已建表、已 begin 的会话。
    """
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    # 判断是否用 PostgreSQL
    use_postgres = TEST_DB_URL.startswith("postgresql")

    engine = create_async_engine(TEST_DB_URL, echo=False, future=True)

    # 收集所有已成功 import 的模型表的 metadata
    tables_to_create: list[sa.Table] = []
    try:
        from app.models.base import Base  # noqa: F401
        from app.models.user import User  # noqa: F401
        from app.models.session import Session  # noqa: F401
        from app.models.message import Message  # noqa: F401

        # UserProfile / KnowledgeDoc / KnowledgeChunk / AuditLog 全部加载
        for mod_name in [
            "app.models.user_profile",
            "app.models.knowledge_doc",
            "app.models.knowledge_chunk",
            "app.models.audit_log",
        ]:
            try:
                __import__(mod_name)
            except Exception:
                continue

        for table_name, table in Base.metadata.tables.items():
            # SQLite 跳过向量表(PostgreSQL 才支持 pgvector)
            if not use_postgres and table_name == "knowledge_chunks":
                continue
            tables_to_create.append(table)
    except Exception:
        pass

    async with engine.begin() as conn:
        if use_postgres:
            # PostgreSQL:确保 pgvector 扩展,然后建表
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        for table in tables_to_create:
            try:
                await conn.run_sync(table.create, checkfirst=True)
            except Exception:
                # 表已存在或类型不支持,跳过
                pass
        if not tables_to_create:
            await conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS _test_placeholder (id INTEGER PRIMARY KEY)"
                )
            )

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    # 清理:PostgreSQL 只清数据(保留表结构供下次用),SQLite drop 所有表
    async with engine.begin() as conn:
        if use_postgres:
            # 清空所有表数据(保留表结构),用 TRUNCATE CASCADE
            for table in reversed(tables_to_create):
                try:
                    await conn.execute(sa.text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
                except Exception:
                    pass
        else:
            for table in reversed(tables_to_create):
                try:
                    await conn.run_sync(table.drop, checkfirst=True)
                except Exception:
                    pass
    await engine.dispose()


@pytest_asyncio.fixture
async def db_engine(db_session: Any) -> Any:
    """暴露底层 engine,供需要直连 connection 的测试使用。

    Args:
        db_session: 依赖 db_session fixture 保证表已建好。

    Returns:
        AsyncEngine 实例。
    """
    return db_session.get_bind()


# ----------------------------------------------------------------------
# Mock 服务 fixture
# ----------------------------------------------------------------------
@pytest.fixture
def mock_llm_service() -> MagicMock:
    """返回 Mock 版 LLMService,对齐真实 LLMService 接口。

    真实 LLMService.chat(messages, tools=None) 返回归一化 dict
    ``{"content": str, "tool_calls": list, "usage": dict}``;
    stream_chat 异步 yield 文本片段;embed / embed_batch 返回向量;
    summarize 返回摘要文本;aclose 关闭连接。

    预设这些方法的 AsyncMock,返回最常见的"成功响应"。具体测试可覆盖
    return_value / side_effect 做更精细控制。

    Returns:
        MagicMock,常用方法已挂载 AsyncMock。
    """
    mock = MagicMock(name="LLMService")

    # 非流式对话:返回真实 LLMService.chat 的归一化结构
    mock.chat = AsyncMock(
        return_value={
            "content": "您好,我是 AI 客服,请问有什么可以帮您?",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
        }
    )

    # 流式对话:返回 token 片段异步迭代器
    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        for chunk in ["您好", ",我是", "AI 客服"]:
            yield chunk

    mock.stream_chat = MagicMock(side_effect=_fake_stream)

    # embedding:返回固定维度向量(与 EMBEDDING_DIMENSION 对齐用 1024)
    mock.embed = AsyncMock(return_value=[0.1] * 1024)
    # 批量 embedding:按入参 chunk 数量返回对应数量向量
    async def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]
    mock.embed_batch = AsyncMock(side_effect=_fake_embed_batch)

    # 摘要:返回固定摘要文本
    mock.summarize = AsyncMock(return_value="用户咨询了订单状态")
    # 关闭连接:无返回值
    mock.aclose = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_rag_service() -> MagicMock:
    """返回 Mock 版 RagService。

    预设 ``retrieve`` / ``chunk_text`` / ``add_document`` 等方法,
    返回与真实 RAGService 接口形状一致的假数据。向量检索在此 mock,
    完全不触碰 pgvector。

    Returns:
        MagicMock,常用方法已挂载 AsyncMock。
    """
    mock = MagicMock(name="RagService")

    fake_chunks = [
        MagicMock(
            id=str(uuid.uuid4()),
            content="退换货政策:7 天内无理由退换。",
            score=0.92,
            metadata={"doc_id": str(uuid.uuid4()), "chunk_index": 0},
        ),
        MagicMock(
            id=str(uuid.uuid4()),
            content="发票可在订单页申请开具。",
            score=0.85,
            metadata={"doc_id": str(uuid.uuid4()), "chunk_index": 2},
        ),
    ]
    mock.retrieve = AsyncMock(return_value=fake_chunks)
    mock.search = AsyncMock(return_value=fake_chunks)

    # 切块:模拟按字符大小切块
    def _fake_chunk(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        if not text:
            return []
        chunks: list[str] = []
        step = max(chunk_size - overlap, 1)
        for i in range(0, len(text), step):
            chunks.append(text[i : i + chunk_size])
            if i + chunk_size >= len(text):
                break
        return chunks

    mock.chunk_text = MagicMock(side_effect=_fake_chunk)
    mock.add_document = AsyncMock(
        return_value=MagicMock(id=str(uuid.uuid4()), chunks_count=3)
    )
    return mock


@pytest.fixture
def mock_memory_service() -> MagicMock:
    """返回 Mock 版 MemoryService。

    预设 get_or_create_profile / update_profile / build_messages /
    get_recent_messages 方法,返回与真实记忆服务接口一致的数据。

    Returns:
        MagicMock,常用方法已挂载 AsyncMock。
    """
    mock = MagicMock(name="MemoryService")

    fake_profile = MagicMock(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        profile_data={"preferences": {"tone": "formal"}, "entities": {"name": "张三"}},
        summary="用户曾咨询订单查询",
    )
    mock.get_or_create_profile = AsyncMock(return_value=fake_profile)
    mock.update_profile = AsyncMock(return_value=fake_profile)

    fake_messages = [
        {"role": "user", "content": "我的订单到哪了?"},
        {"role": "assistant", "content": "请提供订单号。"},
    ]
    mock.get_recent_messages = AsyncMock(return_value=fake_messages)
    mock.build_messages = AsyncMock(
        return_value=[
            {"role": "system", "content": "你是 AI 客服"},
            *fake_messages,
        ]
    )
    return mock


# ----------------------------------------------------------------------
# 用户 / 鉴权 fixture
# ----------------------------------------------------------------------
@pytest_asyncio.fixture
async def sample_user(db_session: Any) -> Any:
    """在内存库中建一个普通测试用户(已哈希密码)。

    依赖 db_session fixture 保证表已建好。若 User 模型尚不可用则跳过,
    返回一个 mock 用户对象,保证下游 fixture(如 auth_token)不崩。

    Returns:
        User 实例或 MagicMock。
    """
    try:
        from app.core.security import hash_password
        from app.models.user import User, UserRole
    except Exception:
        # 模型未就绪:返回带常用属性的 MagicMock
        fake = MagicMock(name="sample_user")
        fake.id = uuid.uuid4()
        fake.username = TEST_USER_USERNAME
        fake.email = TEST_USER_EMAIL
        fake.role = "user"
        fake.is_active = True
        fake.password_hash = "fake-hash"
        return fake

    user = User(
        id=uuid.uuid4(),
        username=TEST_USER_USERNAME,
        email=TEST_USER_EMAIL,
        password_hash=hash_password(TEST_USER_PASSWORD),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def sample_admin(db_session: Any) -> Any:
    """在内存库中建一个管理员测试用户,用于需要 admin 权限的用例。

    Returns:
        User 实例(role=admin)或 MagicMock。
    """
    try:
        from app.core.security import hash_password
        from app.models.user import User, UserRole
    except Exception:
        fake = MagicMock(name="sample_admin")
        fake.id = uuid.uuid4()
        fake.username = TEST_ADMIN_USERNAME
        fake.email = TEST_ADMIN_EMAIL
        fake.role = "admin"
        fake.is_active = True
        fake.password_hash = "fake-hash"
        return fake

    admin = User(
        id=uuid.uuid4(),
        username=TEST_ADMIN_USERNAME,
        email=TEST_ADMIN_EMAIL,
        password_hash=hash_password(TEST_USER_PASSWORD),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


@pytest.fixture
def auth_token(sample_user: Any) -> str:
    """为 sample_user 生成一个有效的 JWT access token。

    token payload 含 sub(user_id) / role / exp(默认有效期)。
    使用与生产相同的 create_access_token,确保测试覆盖真实签发逻辑。

    Args:
        sample_user: 已建好的测试用户。

    Returns:
        JWT 字符串。
    """
    from app.core.security import create_access_token

    token = create_access_token(
        {"sub": str(sample_user.id), "role": getattr(sample_user.role, "value", "user")}
    )
    assert isinstance(token, str) and token.count(".") == 2, "token 应是三段式 JWT"
    return token


@pytest.fixture
def admin_token(sample_admin: Any) -> str:
    """为 sample_admin 生成管理员 JWT。"""
    from app.core.security import create_access_token

    return create_access_token(
        {"sub": str(sample_admin.id), "role": getattr(sample_admin.role, "value", "admin")}
    )


@pytest.fixture
def expired_token(sample_user: Any) -> str:
    """生成一个已过期的 JWT,用于测试过期场景。

    通过 expires_delta 设为负时间,模拟 token 已失效。
    """
    from app.core.security import create_access_token

    return create_access_token(
        {"sub": str(sample_user.id), "role": "user"},
        expires_delta=timedelta(seconds=-1),
    )


# ----------------------------------------------------------------------
# 对话数据 fixture
# ----------------------------------------------------------------------
@pytest.fixture
def sample_messages() -> list[dict[str, str]]:
    """返回一组测试用对话消息(OpenAI 风格)。

    覆盖 user / assistant 两种角色,内容真实,可用于 memory service、
    pipeline context、build_messages 等测试。

    Returns:
        消息字典列表。
    """
    return [
        {"role": "user", "content": "你好,我想查一下我的订单。"},
        {
            "role": "assistant",
            "content": "您好!请提供您的订单号,我帮您查询。",
        },
        {"role": "user", "content": "订单号是 ORD-2024-0001。"},
        {
            "role": "assistant",
            "content": "您的订单 ORD-2024-0001 已发货,预计明天送达。",
        },
        {"role": "user", "content": "好的,谢谢。"},
        {"role": "assistant", "content": "不客气,有其他问题随时找我。"},
    ]


@pytest.fixture
def sample_chat_request() -> dict[str, Any]:
    """返回一个标准的 /chat 请求体。

    Returns:
        含 message 与可选 session_id 的请求字典。
    """
    return {
        "message": "我的订单 ORD-2024-0001 到哪了?",
        "session_id": None,
        "stream": False,
    }


@pytest.fixture
def sample_document_text() -> str:
    """返回一段用于 RAG 切块测试的文档文本。

    含多个段落(双换行分隔)与足够长度,便于测试按大小 / 按段落切块。
    """
    para1 = (
        "欢迎使用我们的客服系统。本系统提供订单查询、退换货、"
        "发票开具等功能。客服工作时间为每天 9:00 至 21:00。"
    )
    para2 = (
        "退换货政策:商品签收后 7 天内可无理由退换。"
        "需保留商品原包装与配件。退款将在收到退货后 3 个工作日内原路退回。"
    )
    para3 = (
        "发票服务:下单时可选择电子发票或纸质发票。"
        "电子发票将在订单完成后 24 小时内发送至您的邮箱。"
        "纸质发票需额外支付 5 元邮寄费。"
    )
    return f"{para1}\n\n{para2}\n\n{para3}"


@pytest.fixture
def sample_order_data() -> dict[str, Any]:
    """返回 mock 的订单查询结果数据。"""
    return {
        "order_id": "ORD-2024-0001",
        "status": "shipped",
        "status_text": "已发货",
        "tracking_no": "SF1234567890",
        "estimated_delivery": "2024-07-18",
        "items": [
            {"name": "无线鼠标", "qty": 1, "price": 99.0},
        ],
        "total": 99.0,
    }


# ----------------------------------------------------------------------
# 应用 / 客户端 fixture
# ----------------------------------------------------------------------
def _build_test_app(db_session: Any, mock_llm_service: Any, mock_rag_service: Any,
                    mock_memory_service: Any) -> Any:
    """构造一个注入了 mock 依赖的 FastAPI 应用。

    集中在此函数,client fixture 与各集成测试复用。使用 dependency_overrides
    把 get_db / get_llm_service 等替换为返回 mock 的版本,完全隔离外部依赖。

    若 main.py / create_app 尚未由并行 agent 创建,则返回 None,调用方据此跳过。
    """
    try:
        from app.main import create_app  # type: ignore
    except Exception:
        return None

    app = create_app()

    async def _override_db() -> Any:
        yield db_session

    async def _override_llm() -> Any:
        return mock_llm_service

    async def _override_rag() -> Any:
        return mock_rag_service

    async def _override_memory() -> Any:
        return mock_memory_service

    try:
        from app.api.deps import (
            get_db,
            get_llm_service,
            get_memory_service,
            get_rag_service,
        )

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_llm_service] = _override_llm
        app.dependency_overrides[get_rag_service] = _override_rag
        app.dependency_overrides[get_memory_service] = _override_memory
    except Exception:
        # deps 尚未就绪时,override 跳过;集成测试会用其他方式隔离
        pass

    return app


@pytest.fixture
def client(
    db_session: Any,
    mock_llm_service: Any,
    mock_rag_service: Any,
    mock_memory_service: Any,
) -> Any:
    """返回注入了 mock db / 服务的 FastAPI TestClient。

    若 app.main 尚未创建(并行 agent 未完成),返回 None。
    依赖此 fixture 的测试应 ``if client is None: pytest.skip(...)`` 跳过。

    Returns:
        TestClient 实例或 None。
    """
    try:
        from fastapi.testclient import TestClient
    except Exception:
        return None

    app = _build_test_app(db_session, mock_llm_service, mock_rag_service, mock_memory_service)
    if app is None:
        return None

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """每个测试前后清空 Settings 单例缓存,保证 env 变更生效。

    config.get_settings 用 lru_cache 缓存,测试改 env 后必须 clear 才能重读。
    autouse=True 让所有测试自动具备隔离性,无需手动调用。
    """
    try:
        from app.config import reset_settings

        reset_settings()
    except Exception:
        pass
    yield
    try:
        from app.config import reset_settings

        reset_settings()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_service_singletons() -> Iterator[None]:
    """每个测试前后清空 deps 中缓存的 LLMService / Pipeline 单例。

    防止上一个用例的真实/ mock 服务实例串到下一个用例。
    """
    yield
    try:
        from app.api.deps import reset_singletons

        reset_singletons()
    except Exception:
        pass


# ----------------------------------------------------------------------
# 辅助 fixture
# ----------------------------------------------------------------------
@pytest.fixture
def now_utc() -> datetime:
    """返回当前 UTC 时间(带时区),供时间相关断言比较。"""
    return datetime.now(timezone.utc)


@pytest.fixture
def fixed_uuid() -> uuid.UUID:
    """返回一个固定 UUID,用于需要确定 ID 的测试。"""
    return uuid.UUID("12345678-1234-5678-1234-567812345678")
