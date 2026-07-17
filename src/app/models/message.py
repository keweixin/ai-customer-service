"""消息模型:会话中的单条对话消息。

覆盖 system / user / assistant / tool 四种角色,兼容 OpenAI 风格消息流,
便于直接喂给 LLM。tokens_used 用于成本核算与限流;metadata 存放工具调用、
引用的知识块 ID 等结构化扩展,避免频繁加列。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .session import Session


class MessageRole(str, enum.Enum):
    """消息角色,对齐 OpenAI ChatCompletion 消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # 工具调用结果回传


class Message(Base):
    """消息表:对话流水。"""

    __tablename__ = "messages"

    # 注意:Message 不继承 TimestampMixin,因为它只需要 created_at(无更新语义,
    # 消息一旦写入即不可变,体现"对话流水只追加"的设计原则)。
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # ondelete=CASCADE:删会话连带删消息。
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话",
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="消息角色",
    )
    content: Mapped[str] = mapped_column(
        String, nullable=False, comment="消息文本内容"
    )
    tokens_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="本条消息消耗的 token 数(成本核算/限流)",
    )
    # JSONB 而非 JSON:PostgreSQL 原生二进制 JSON,支持索引与高效查询,
    # 存放 tool_calls / 引用的知识块 ID / 模型名等可变扩展字段。
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="扩展元数据(工具调用、引用块等)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="消息创建时间",
    )

    # ---- relationships ----
    session: Mapped["Session"] = relationship(back_populates="messages")

    # ---- 索引 ----
    # 复合索引(session_id, created_at):对话回放最常见查询是
    # "按时间正序取某会话消息",该复合索引同时覆盖等值过滤与排序,
    # 避免文件排序;遵循最左前缀,单独按 session_id 过滤也能命中。
    __table_args__ = (
        Index(
            "ix_messages_session_created",
            "session_id",
            "created_at",
        ),
    )
