"""SQLAlchemy 声明式基类与公共 mixin。

本模块定义所有 ORM 模型的统一基类与公共字段,约定:
- 全部使用 SQLAlchemy 2.0 风格(Mapped / mapped_column)与类型注解;
- 主键统一用 UUID(见各模型),避免自增整数在分库分表/迁移时冲突;
- 时间字段统一带时区(timezone=True),防止跨时区部署时间错乱;
- TimestampMixin 提供 created_at / updated_at,updated_at 由数据库层
  触发器维护(见迁移文件),保证即便绕过 ORM 更新也能刷新时间戳。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。

    2.0 风格直接继承 DeclarativeBase 即可,无需 declarative_base() 工厂。
    metadata 在此处集中维护,Alembic env.py 通过 Base.metadata 驱动迁移。
    """

    def to_dict(self) -> dict[str, Any]:
        """将模型实例转为普通字典(用于 API 序列化 / 日志)。

        注意:不做深度递归,relationship 字段需各模型自行处理或用 Pydantic
        schema 转换,避免懒加载在异步上下文中触发隐式 IO。
        """
        result: dict[str, Any] = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            # datetime 统一转 ISO8601 字符串,方便 JSON 序列化。
            if isinstance(value, datetime):
                result[column.name] = value.isoformat()
            else:
                result[column.name] = value
        return result


class TimestampMixin:
    """公共时间字段 mixin:created_at / updated_at。

    - created_at:插入时由数据库 DEFAULT now() 写入,不再由应用层赋值,
      保证多入口(脚本、迁移、直连)写入一致;
    - updated_at:同样新建时取 now(),并在更新时由触发器刷新(见迁移)。
      这里不用 Python 端 onupdate,是为了让裸 SQL 更新也能正确维护。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="记录创建时间(UTC,带时区)",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="记录最后更新时间(由触发器维护)",
    )
