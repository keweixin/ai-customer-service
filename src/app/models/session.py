"""会话模型:一次完整客服对话的容器。

一个用户可有多场会话;一场会话含多条消息。会话状态机:
active -> closed(正常结束)/ transferred(转人工)。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .message import Message
    from .user import User


class SessionStatus(str, enum.Enum):
    """会话状态。

    用枚举而非裸字符串,防止拼写错误导致状态机失灵;
    继承 str 便于直接序列化进 API 响应。
    """

    ACTIVE = "active"            # 进行中
    CLOSED = "closed"            # 正常关闭
    TRANSFERRED = "transferred"  # 已转人工客服


class Session(Base, TimestampMixin):
    """会话表:记录一场客服对话的元信息与生命周期。"""

    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 外键指向 users.id。ondelete=CASCADE:删用户时连带删其会话,
    # 防止孤儿会话;与会话侧的 cascade="all, delete-orphan" 配合。
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属用户",
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SessionStatus.ACTIVE,
        server_default=SessionStatus.ACTIVE.value,
        index=True,
        comment="会话状态",
    )
    # 业务时间字段(区别于 created_at 的入库时间):
    # started_at 在会话真正开始(首条消息)时写入;ended_at 在关闭/转人工时写入。
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="会话开始时间"
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="会话结束时间"
    )
    # 对话摘要:当消息数超过阈值时,MemoryService 把更早的消息压缩成一段摘要
    # 写回这里(见 MemoryService.summarize_if_too_long)。后续组装上下文时,
    # 该摘要作为"长期记忆"注入 system prompt,避免无界增长的历史消息撑爆 token。
    # 用 Text 而非 String:摘要长度不定,Text 无长度上限约束。
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="历史消息摘要(超长时压缩,注入 system prompt)"
    )

    # ---- relationships ----
    user: Mapped["User"] = relationship(back_populates="sessions")
    # 一对多:一场会话多条消息。删会话连带删消息(cascade all, delete-orphan)。
    messages: Mapped[List["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",  # 默认按时间正序,契合对话回放语义
        lazy="selectin",
    )
