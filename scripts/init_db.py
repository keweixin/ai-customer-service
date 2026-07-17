#!/usr/bin/env python3
"""数据库初始化脚本。

功能:
1. 连接 PostgreSQL(用配置中的连接串)。
2. 启用 pgvector 扩展(RAG 向量检索依赖)。
3. 执行 alembic upgrade head(建表/索引/触发器)。
4. 创建默认管理员账号(用户名/密码可配,默认 admin/admin)。
5. 打印每步结果,便于确认初始化是否成功。

用法:
    python scripts/init_db.py
    python scripts/init_db.py --admin-username root --admin-password S3cret!
    python scripts/init_db.py --skip-alembic          # 只建扩展+建管理员
    python scripts/init_db.py --help

前置条件:
    - PostgreSQL 已启动并可连接(见 docker-compose.yml)。
    - 已 pip install -e ".[dev]",依赖可用。
    - .env 已配置 DATABASE_URL / POSTGRES_*。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 把项目 src 加入 sys.path,便于直接 import app.*
# 脚本可能从仓库根目录或 scripts/ 目录运行,统一处理。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="init_db",
        description="初始化 AI 客服系统数据库:启用 pgvector + alembic 迁移 + 创建默认管理员。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--admin-username",
        default="admin",
        help="默认管理员用户名",
    )
    parser.add_argument(
        "--admin-email",
        default="admin@example.com",
        help="默认管理员邮箱",
    )
    parser.add_argument(
        "--admin-password",
        default="admin",
        help="默认管理员密码(仅初始化用,生产务必立即改!)",
    )
    parser.add_argument(
        "--skip-alembic",
        action="store_true",
        help="跳过 alembic upgrade head(假定表已建好)",
    )
    parser.add_argument(
        "--skip-admin",
        action="store_true",
        help="跳过创建默认管理员",
    )
    return parser.parse_args()


def _enable_pgvector(sync_url: str) -> None:
    """启用 pgvector 扩展。

    用同步 psycopg 连接(简单可靠),CREATE EXTENSION 需超级用户或已授权。
    连接串从 asyncpg 格式(postgresql+asyncpg://)转 psycopg 格式(postgresql://)。
    """
    import psycopg  # 延迟 import,脚本启动更快且依赖缺失时报错清晰

    # asyncpg driver -> psycopg driver
    psycopg_url = sync_url.replace("+asyncpg", "")
    print(f"[1/3] 启用 pgvector 扩展(连接: {_mask_url(psycopg_url)}) ...")
    try:
        with psycopg.connect(psycopg_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(
                    "SELECT extname FROM pg_extension WHERE extname = 'vector';"
                )
                row = cur.fetchone()
                if row is None:
                    print("  [失败] pgvector 扩展未启用,请确认镜像为 pgvector/pgvector:pg15")
                    sys.exit(1)
                print(f"  [成功] 扩展 '{row[0]}' 已启用")
    except psycopg.OperationalError as e:
        print(f"  [失败] 无法连接数据库: {e}")
        print("  请确认 PostgreSQL 已启动且 DATABASE_URL 配置正确")
        sys.exit(1)


def _run_alembic() -> None:
    """执行 alembic upgrade head。"""
    from alembic import command
    from alembic.config import Config

    print("[2/3] 执行 alembic upgrade head ...")
    ini_path = _PROJECT_ROOT / "alembic.ini"
    if not ini_path.exists():
        print(f"  [失败] 未找到 alembic.ini: {ini_path}")
        print("  请先运行: alembic init migrations")
        sys.exit(1)

    cfg = Config(str(ini_path))
    # 显式指定 migrations 目录(alembic.ini 可能用相对路径)
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    try:
        command.upgrade(cfg, "head")
        print("  [成功] 迁移已应用到最新版本")
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] alembic 迁移失败: {e}")
        sys.exit(1)


async def _create_admin(username: str, email: str, password: str) -> None:
    """创建默认管理员账号(已存在则跳过)。

    用项目自身的 SQLAlchemy async session + User 模型 + security.hash_password,
    保证密码哈希与运行时完全一致。
    """
    from sqlalchemy import select

    from app.config import get_settings
    from app.core.security import hash_password
    from app.models.user import User, UserRole
    from app.models.base import Base  # noqa: F401  确保 metadata 注册
    # 延迟 import repositories/数据库引擎,按实际项目结构调整
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

    print(f"[3/3] 创建默认管理员({username}) ...")
    settings = get_settings()
    engine = create_async_engine(settings.database.url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with SessionLocal() as session:
            # 先查是否已存在(按用户名或邮箱)
            stmt = select(User).where(
                (User.username == username) | (User.email == email)
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                print(f"  [跳过] 管理员已存在: username={existing.username} email={existing.email}")
                return

            admin = User(
                username=username,
                email=email,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(admin)
            await session.commit()
            print(f"  [成功] 管理员已创建: username={username} role=admin")
            print("  ⚠ 警告:默认密码为 admin,生产环境请立即修改!")
    finally:
        await engine.dispose()


def _mask_url(url: str) -> str:
    """脱敏连接串中的密码,日志安全。"""
    import re

    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


async def _amain(args: argparse.Namespace) -> None:
    """异步主流程入口。"""
    from app.config import get_settings

    settings = get_settings()
    db_url = settings.database.url

    _enable_pgvector(db_url)

    if not args.skip_alembic:
        _run_alembic()
    else:
        print("[2/3] 跳过 alembic(--skip-alembic)")

    if not args.skip_admin:
        await _create_admin(args.admin_username, args.admin_email, args.admin_password)
    else:
        print("[3/3] 跳过创建管理员(--skip-admin)")

    print("\n数据库初始化完成。")


def main() -> None:
    """脚本入口。"""
    args = _parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] 初始化失败: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
