"""消息 Repository:封装 messages 表的数据访问。

对齐 API 层调用契约(chat.py / admin.py):
- ``MessageRepository(db).create(session_id=, role=, content=)``
- ``MessageRepository(db).list_by_session(session_id, limit=)`` -- 历史消息(正序)
- ``MessageRepository(db).count()`` -- 管理后台统计

另提供任务要求的:
- ``get_recent(session_id, limit)`` -- 取最近 N 条并按时间正序返回(滑动窗口)
- ``count_by_session(session_id)`` -- 判断是否需要摘要压缩
- ``delete_older_than(session_id, keep_count)`` -- 摘要后删除已压缩的早期消息

注意:
- Message 模型字段为 ``tokens_used``(非 tokens),``role`` 为 MessageRole 枚举;
- create 接收 role 为 str(调用方传 "user"/"assistant"),内部转枚举,降低调用方耦合;
- 消息表只有 created_at(只追加,无 updated_at),故不继承 TimestampMixin。
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.message import Message, MessageRole
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class MessageRepository(BaseRepository[Message]):
    """消息表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(Message, db)

    async def create(
        self,
        *,
        session_id: UUID,
        role: str,
        content: str,
        tokens_used: int = 0,
        metadata: Optional[dict[str, object]] = None,
    ) -> Message:
        """写入一条消息。

        Args:
            session_id: 所属会话。
            role: 消息角色,字符串形式("user"/"assistant"/"system"/"tool"),
                  内部转 MessageRole 枚举;非法值抛 ValueError。
            content: 消息正文。
            tokens_used: 本条消息 token 数(成本核算/上下文裁剪);默认 0。
            metadata: 扩展元信息(工具调用/引用块 ID 等),可选。
        """
        # 字符串 -> 枚举:统一在仓库层转,调用方(auth/chat)无需 import 枚举。
        try:
            role_enum = MessageRole(role)
        except ValueError as exc:
            raise ValueError(f"非法消息角色: {role!r}") from exc

        message = await super().create(
            {
                "session_id": session_id,
                "role": role_enum,
                "content": content,
                "tokens_used": tokens_used,
                "metadata_": metadata,  # 模型属性名带下划线避开保留字
            }  # type: ignore[arg-type]
        )
        _logger.debug(
            "消息已写入",
            message_id=str(message.id),
            session_id=str(session_id),
            role=role,
        )
        return message

    async def list_by_session(
        self, session_id: UUID, *, limit: int = 100
    ) -> list[Message]:
        """按时间正序返回会话消息(对话回放语义)。

        复合索引 (session_id, created_at) 命中,等值过滤 + 排序一次完成。
        """
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_recent(
        self, session_id: UUID, limit: int = 20
    ) -> list[Message]:
        """取最近 N 条消息并按时间正序返回(滑动窗口)。

        实现先 DESC 取 limit 条(拿到最近的),再 reverse 成正序,
        使拼装 LLM messages 时顺序正确(system -> 旧 -> 新)。
        """
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        recent_desc = list(result.scalars().all())
        recent_desc.reverse()
        return recent_desc

    async def count_by_session(self, session_id: UUID) -> int:
        """统计某会话的消息数,用于判断是否触发摘要压缩。"""
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def delete_older_than(self, session_id: UUID, keep_count: int) -> int:
        """删除某会话中较早的消息,仅保留最近 keep_count 条。

        供 MemoryService.summarize_if_too_long 在摘要后清理已压缩消息使用。
        实现用子查询定位"要保留的最近 N 条"之外的消息 ID,再批量删除,
        避免取回全部消息到内存。

        Args:
            session_id: 目标会话。
            keep_count: 保留最近的消息条数;<=0 表示全部删除。

        Returns:
            实际删除的行数。
        """
        # 子查询:取最近 keep_count 条的 id(DESC limit),DELETE 取补集。
        keep_ids_subq = (
            select(Message.id)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(keep_count)
            .subquery()
        )
        # NOT IN 删除"非保留"的消息;~(id IN keep_ids) 等价。
        stmt = (
            delete(Message)
            .where(Message.session_id == session_id)
            .where(Message.id.not_in(select(keep_ids_subq.c.id)))
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        deleted = int(result.rowcount or 0)
        _logger.info(
            "已删除早期消息",
            session_id=str(session_id),
            kept=keep_count,
            deleted=deleted,
        )
        return deleted
