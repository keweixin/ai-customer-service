"""Alembic 迁移环境配置。

职责:
- 从环境变量 DATABASE_URL 读取连接串(覆盖 alembic.ini 中的空 url);
- 导入 app.models 全部模型,使 Base.metadata 包含所有表(autogenerate 前提);
- 在迁移前执行 CREATE EXTENSION vector,保证 pgvector 类型可用;
- 支持 offline(生成 SQL)与 online(直连执行)两种模式。

注意:Alembic 原生 run_migrations_online 用同步连接。我们的 DATABASE_URL 是
asyncpg 协议(postgresql+asyncpg://),这里在同步迁移时改写为 psycopg2 同步驱动,
避免引入异步迁移的复杂度。生产若用 asyncpg 迁移需自行实现 async online runner。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# 导入 Base 与全部模型:导入即注册到 metadata,autogenerate 才能发现所有表。
# 必须显式 import models 包(而非只 import Base),否则未导入的模型不会被注册。
from app.models import Base  # noqa: F401  pylint: disable=unused-import
import app.models  # noqa: F401  pylint: disable=unused-import

# Alembic 配置对象
config = context.config

# 应用日志配置(若 alembic.ini 中定义了)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 元数据目标:autogenerate 据此对比数据库现状生成差异。
target_metadata = Base.metadata


def _get_database_url() -> str:
    """获取同步数据库连接串。

    DATABASE_URL 形如 postgresql+asyncpg://user:pass@host:port/db,
    Alembic 同步迁移需替换为 psycopg2 驱动:
        postgresql+asyncpg:// -> postgresql+psycopg2://
    这样同一份环境变量同时服务应用(异步)与迁移(同步),无需维护两套连接串。
    """
    # 先尝试环境变量,再从 .env 加载(alembic 不自动加载 .env)
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            from dotenv import load_dotenv
            # 从项目根目录(alembic.ini 所在目录)读 .env
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dotenv_path = os.path.join(project_root, ".env")
            if os.path.exists(dotenv_path):
                load_dotenv(dotenv_path)
                url = os.environ.get("DATABASE_URL")
        except ImportError:
            pass
    if not url:
        raise RuntimeError(
            "DATABASE_URL 环境变量未设置,Alembic 无法连接数据库。"
            "请在 .env 中配置 DATABASE_URL,或安装 python-dotenv。"
        )
    return url.replace("+asyncpg", "+psycopg2")


# 把解析出的 url 写回 config,供 engine_from_config 使用。
config.set_main_option("sqlalchemy.url", _get_database_url())


def run_migrations_offline() -> None:
    """离线模式:不连接数据库,仅生成 SQL 脚本。

    适用于:CI 中预审迁移 SQL、DBA 人工审核、无网络环境准备脚本。
    用 literal_binds 把参数渲染成字面量,生成可独立执行的 SQL。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 比较类型与服务器默认值,使 autogenerate 更精确地检测列变更。
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        # 先输出启用 pgvector 扩展的 SQL:建表脚本依赖 vector 类型。
        context.execute("CREATE EXTENSION IF NOT EXISTS vector")
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式:连接数据库并执行迁移。

    用 engine_from_config + begin 事务一次性执行,出错整体回滚。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # 迁移用空连接池,避免长连接干扰
    )

    with connectable.connect() as connection:
        # 先启用 pgvector 扩展:knowledge_chunks.embedding 依赖 vector 类型。
        # CREATE EXTENSION 需在事务外或事务内均可(PG 13+),此处放在连接上执行。
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


# Alembic 入口:根据命令行 --sql 决定 offline/online。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
