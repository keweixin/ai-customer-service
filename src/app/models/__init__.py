"""ORM 模型集合。

集中导出所有模型类与 Base,使 Alembic env.py 与应用代码只需
`from app.models import Base, User, ...` 即可拿到全部元数据。
导入本包即触发所有模型注册到 Base.metadata,这是迁移能发现所有表的前提。
"""

from __future__ import annotations

from .base import Base, TimestampMixin
from .user import User, UserRole
from .session import Session, SessionStatus
from .message import Message, MessageRole
from .user_profile import UserProfile
from .knowledge_doc import KnowledgeDoc, SourceType
from .knowledge_chunk import KnowledgeChunk, EMBEDDING_DIMENSION
from .audit_log import AuditLog

__all__ = [
    # 基类与 mixin
    "Base",
    "TimestampMixin",
    # 用户与画像
    "User",
    "UserRole",
    "UserProfile",
    # 会话与消息
    "Session",
    "SessionStatus",
    "Message",
    "MessageRole",
    # 知识库
    "KnowledgeDoc",
    "SourceType",
    "KnowledgeChunk",
    "EMBEDDING_DIMENSION",
    # 审计
    "AuditLog",
]
