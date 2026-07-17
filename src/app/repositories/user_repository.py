"""用户 Repository:封装 users 表的数据访问。

对齐 API 层调用契约(auth.py / admin.py / deps.py):
- ``UserRepository(db).get_by_username(username)`` -- 登录/注册唯一性预检;
- ``UserRepository(db).get_by_email(email)`` -- 注册邮箱唯一性预检;
- ``UserRepository(db).create(username=, email=, password_hash=)`` -- 注册;
- ``UserRepository(db).list_all(limit=, offset=)`` / ``count()`` -- 管理后台。

create 接收关键字参数而非 dict,是因为调用方(auth.register)直接传业务字段,
关键字形式可读性更好且能在签名层暴露必填项;内部转 dict 复用基类。
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.user import User
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class UserRepository(BaseRepository[User]):
    """用户表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(User, db)

    async def get_by_username(self, username: str) -> Optional[User]:
        """按用户名查询(登录与注册唯一性预检)。

        username 列有唯一索引,查询走索引,单条返回。
        """
        stmt = select(User).where(User.username == username)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[User]:
        """按邮箱查询(注册邮箱唯一性预检)。"""
        stmt = select(User).where(User.email == email)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        role: str | None = None,
        is_active: bool = True,
    ) -> User:
        """创建用户。

        关键字参数形式:调用方(auth.register)直接传业务字段,签名即文档。
        role 缺省由模型 server_default 兜底为 user,这里允许显式覆盖(建管理员)。
        """
        data: dict[str, object] = {
            "username": username,
            "email": email,
            "password_hash": password_hash,
            "is_active": is_active,
        }
        if role is not None:
            data["role"] = role
        user = await super().create(data)  # type: ignore[arg-type]
        _logger.info("用户已创建", user_id=str(user.id), username=username)
        return user

    async def update_password(self, user_id: UUID, password_hash: str) -> Optional[User]:
        """更新密码哈希(改密场景)。"""
        return await self.update(user_id, {"password_hash": password_hash})

    async def set_active(self, user_id: UUID, is_active: bool) -> Optional[User]:
        """启用/禁用账号(软禁用)。"""
        return await self.update(user_id, {"is_active": is_active})
