"""用户模型。

承载客服系统的终端用户与管理员账号。密码仅存哈希(passlib[bcrypt]),
绝不存明文。角色用枚举约束,避免脏数据。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, List
from uuid import UUID

from sqlalchemy import Boolean, Enum, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .session import Session
    from .user_profile import UserProfile


class UserRole(str, enum.Enum):
    """用户角色枚举。

    继承 str 便于直接 JSON 序列化;值与 API/JWT claim 中的 role 字段对齐。
    """

    USER = "user"       # 普通客服用户
    ADMIN = "admin"     # 管理员(可上传知识库文档、查审计日志)


class User(Base, TimestampMixin):
    """用户表:统一存储终端用户与管理员。"""

    __tablename__ = "users"

    # 主键用客户端生成 UUID(default=uuid4),分布式中可提前生成 ID,
    # 避免依赖数据库 RETURNING 回填,便于异步写入与事件溯源。
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="登录用户名"
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False, comment="邮箱(唯一)"
    )
    # bcrypt 哈希固定 60 字符,预留 128 容纳算法升级。
    password_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="bcrypt 密码哈希,非明文"
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.USER,
        server_default=UserRole.USER.value,
        comment="用户角色",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="账号是否启用(软禁用用)",
    )

    # ---- relationships ----
    # 一对多:一个用户多场会话。cascade 设为 all, delete-orphan,
    # 删用户时连带清理会话(敏感数据不应残留)。
    sessions: Mapped[List["Session"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # 一对一:用户画像(长期记忆)。
    profile: Mapped["UserProfile"] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
