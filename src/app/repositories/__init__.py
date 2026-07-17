"""Repository 层:数据访问封装。

业务 service 层通过 repository 操作数据库,而非直接持 session 写 ORM,
从而隔离持久化细节、便于单测 mock。各实体 repository 继承 BaseRepository
(构造期注入 db,方法签名不再每处传 db,契合 ``Repo(db).method(args)`` 调用风格)。

模块清单:
- base: 泛型 BaseRepository(CRUD + count + 批量删除)
- user_repository: 用户表
- session_repository: 会话表(含 summary / mark_started / close)
- message_repository: 消息表(滑动窗口 / 摘要后清理)
- profile_repository: 用户画像(JSONB || merge / ON CONFLICT upsert)
- document_repository: 知识库文档表
- knowledge_chunk_repository: 知识库切块表(pgvector <=> 检索 / ILIKE 兜底)
- audit_log_repository: 审计日志(只追加,前缀过滤统计)
"""

from __future__ import annotations

from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.base import BaseRepository
from app.repositories.document_repository import DocumentRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.profile_repository import UserProfileRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "BaseRepository",
    "UserRepository",
    "SessionRepository",
    "MessageRepository",
    "UserProfileRepository",
    "DocumentRepository",
    "KnowledgeChunkRepository",
    "AuditLogRepository",
]
