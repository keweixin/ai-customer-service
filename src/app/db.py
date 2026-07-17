"""数据库连接与会话管理。

职责:
- 创建全局 async engine(带连接池参数与 pool_pre_ping 心跳);
- 提供 async_sessionmaker 供业务层获取会话;
- get_db() 作为 FastAPI Depends 注入点,确保每请求一会话且自动关闭;
- init_db() 开发期建表 + 启用 pgvector 扩展(生产用 Alembic 迁移)。

配置来源:优先从 app.core.config.settings.DATABASE_URL 读取(若该模块已实现);
否则回退到环境变量 DATABASE_URL。这样在 core/config 尚未落地时也能独立可用,
且不与之强耦合。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 延迟导入 settings 以解耦:core/config 可能尚未实现,但 DATABASE_URL 环境变量
# 已由 .env.example 约定存在。两路取值保证本模块在任何阶段都能工作。


def _resolve_database_url() -> str:
    """解析数据库连接串。

    优先用 ``app.config.get_settings().database.url``(已聚合 DATABASE_URL 或
    单字段拼装),否则回退到环境变量。这样既支持配置中心化(settings),又保证在
    配置缺失/异常时仍可独立运行(测试/早期开发)。
    """
    try:
        from app.config import get_settings

        url: Optional[str] = get_settings().database.url
        if url:
            return url
    except Exception:
        # config 模块导入失败或配置不完整,走环境变量回退,不阻断启动。
        pass

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置:请在 .env 中设置 DATABASE_URL,或实现 "
            "app.config.Settings.database.url"
        )
    return url


DATABASE_URL: str = _resolve_database_url()

# ---- async engine ----
# pool_size/max_overflow:连接池常驻 + 溢出上限,按并发量调优;
# pool_pre_ping=True:每次取连接前发 ping,避免拿到已被数据库侧关闭的死连接
#   (长空闲连接被防火墙/DB 重启切断的常见坑);
# pool_recycle:连接最大存活时间,小于 DB 侧 wait_timeout 防止使用过期连接。
engine = create_async_engine(
    DATABASE_URL,
    echo=False,             # 生产关闭 SQL 回显;调试时可设 True
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,      # 30 分钟,小于常见 PG idle 超时
)

# ---- session factory ----
# expire_on_commit=False:提交后对象属性不过期,避免异步上下文中再次访问属性
# 触发隐式 IO(异步 + 懒加载是常见坑,默认 True 会在 commit 后 refresh,
# 在已关闭的会话上访问会报 MissingGreenlet)。
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,         # 显式 flush,避免查询前隐式 flush 带来意外 SQL
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends 注入点:每请求一个会话,请求结束自动关闭。

    用法:
        @app.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...

    异常时通过 finally 保证 session.close() 一定执行,防止连接泄漏;
    不在此 commit/rollback--事务边界交由 service 层显式控制,
    避免框架层隐式提交掩盖业务逻辑错误。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """开发期初始化:启用 pgvector 扩展 + 建所有表。

    生产环境请用 Alembic 迁移(migrations/),不要用本方法建表:
    metadata.create_all 不处理增量变更与索引细节(如 HNSW 参数)。
    本方法主要用于快速起服务/测试建库。
    """
    # 必须先启用扩展:pgvector 类型 vector 才存在,否则建 knowledge_chunks 表失败。
    from sqlalchemy import text

    # 导入 models 包以触发所有模型注册到 Base.metadata。
    from app import models  # noqa: F401  pylint: disable=unused-import

    async with engine.begin() as conn:
        # CREATE EXTENSION 若已存在则跳过(IF NOT EXISTS),可重复执行。
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(models.Base.metadata.create_all)
