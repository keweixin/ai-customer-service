"""用户画像模型(长期记忆)。

存放跨会话的持久化用户信息:偏好、实体(姓名/地址/订单号)、行为画像等,
以及对话摘要(summary)。RAG/上下文组装时优先取这里,避免每轮都把全部历史
消息塞进 prompt,降低 token 成本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .user import User


class UserProfile(Base, TimestampMixin):
    """用户画像表:与用户一对一。

    只用 updated_at(画像持续刷新),created_at 由 TimestampMixin 提供。
    """

    __tablename__ = "user_profiles"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 一对一:user_id 加 UNIQUE 约束。ondelete=CASCADE,删用户连带删画像,
    # 避免画像指向不存在的用户。
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
        comment="所属用户(一对一)",
    )
    # profile_data 用 JSONB:画像结构随业务演进(偏好/实体/标签),用 schema-less
    # 字段避免频繁加列;JSONB 支持 GIN 索引做结构化查询。
    # 结构示例:
    #   {"preferences": {"tone": "formal"}, "entities": {"name": "张三"},
    #    "traits": ["价格敏感", "夜间活跃"]}
    profile_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="用户画像数据(偏好/实体/标签)",
    )
    # summary:LLM 生成的对话摘要,作为长期记忆的压缩表示。
    # Text 而非 String:摘要长度不定,Text 无长度上限约束。
    summary: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="对话摘要(长期记忆压缩)"
    )

    # ---- relationships ----
    user: Mapped["User"] = relationship(back_populates="profile")
