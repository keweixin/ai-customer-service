"""审计日志模型:记录关键安全/业务动作,用于合规追溯。

审计日志是"只追加"流水:写入后不修改、不删除(故无 updated_at,且 user_id
用 SET NULL 而非 CASCADE--删用户后仍需保留其历史审计痕迹)。

设计说明(与 API 层契约对齐):
- ``action`` 用 String 而非枚举:API 层(见 app.api.v1.auth/admin)写入的动作是
  任意字符串点分形式(``user.register`` / ``user.login`` / ``llm.chat`` …),
  且管理后台支持按 action 前缀(如 ``llm.``)聚合统计。枚举无法承载开放动作集,
  因此退化为带索引的字符串列,由调用方约定命名规范。
- ``target`` / ``detail`` / ``ip_address`` 保留,字段名稳定,便于运营按多维度排查。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLog(Base):
    """审计日志表:只追加,不更新不删除。"""

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 关键设计:ondelete=SET NULL 而非 CASCADE。
    # 理由:审计日志的目的是事后追溯,即便用户被删除,其历史行为痕迹必须保留。
    # 因此删用户时把 user_id 置空,记录本身不动。
    # 同时暴露属性别名 ``actor_id``:API 层(AuditLogRepository.create)用
    # ``actor_id=`` 关键字写入,语义上"操作发起者"比"用户"更贴切,
    # 用 synonym 让两个名字指向同一列,避免在仓库层做字段名翻译。
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="操作者(删用户后置空以保留历史)",
    )
    # action 为字符串而非枚举:见模块 docstring--动作集是开放的(点分命名),
    # 管理后台按前缀过滤,枚举无法承载。加索引便于按动作类型聚合筛查。
    action: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="动作标识(点分字符串,如 user.register / llm.chat)",
    )
    # target:动作作用对象的可读标识(如文档标题/工具名/转交的会话 ID),
    # 用字符串而非外键,避免与具体表耦合、且删对象后审计仍可读。
    target: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, comment="动作目标(可读标识)"
    )
    # detail:结构化详情(登录方式/工具入参/转交原因等)。
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True, comment="动作详情(结构化)"
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),  # IPv6 最长 45 字符
        nullable=True,
        comment="操作来源 IP(v4/v6)",
    )
    # 简要结果信息(成功 / 失败原因),便于日志快速过滤。
    message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="操作结果摘要"
    )
    # 仅 created_at,无 updated_at:审计日志只追加,永不更新。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        index=True,
        comment="动作发生时间",
    )
