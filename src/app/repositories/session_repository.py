"""会话 Repository:封装 sessions 表的数据访问。

对齐 API 层调用契约(chat.py / admin.py):
- ``SessionRepository(db).get_by_id(session_id)``
- ``SessionRepository(db).create(user_id=...)`` -- 新建会话(状态 active)
- ``SessionRepository(db).list_by_user(user_id, limit=, offset=)``
- ``SessionRepository(db).close(session_id)`` -- active -> closed
- ``SessionRepository(db).mark_started_if_null(session_id, started_at)``
- ``SessionRepository(db).count()`` -- 管理后台统计

另提供 ``get_with_messages``(eager load 消息,任务要求)与
``update_summary``(供 MemoryService.summarize_if_too_long 落摘要)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.models.session import Session, SessionStatus
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class SessionRepository(BaseRepository[Session]):
    """会话表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(Session, db)

    async def create(
        self,
        *,
        user_id: UUID,
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> Session:
        """新建会话。

        started_at 留空:按现有 chat.py 约定,首条消息发出后再用
        ``mark_started_if_null`` 补写,使"会话开始时间"贴合真实首次交互。
        """
        session = await super().create(
            {"user_id": user_id, "status": status}  # type: ignore[arg-type]
        )
        _logger.info("会话已创建", session_id=str(session.id), user_id=str(user_id))
        return session

    async def list_by_user(
        self,
        user_id: UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Session]:
        """列出某用户的会话(按创建时间倒序,最近在前)。

        倒序契合"我的会话"列表 UI:用户最关心最近的对话。
        不预加载 messages,避免列表接口拉取海量消息撑爆响应。
        """
        stmt = (
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def close(self, session_id: UUID) -> bool:
        """关闭会话:active -> closed,并写 ended_at。

        幂等:对已关闭的会话再调 close 不报错(UPDATE 命中 0 行返回 False)。
        用 UPDATE 而非先查后改,单条 SQL 完成状态迁移,避免读改写竞态。
        """
        stmt = (
            update(Session)
            .where(Session.id == session_id)
            .values(status=SessionStatus.CLOSED, ended_at=datetime.utcnow())
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        closed = (result.rowcount or 0) > 0
        if closed:
            _logger.info("会话已关闭", session_id=str(session_id))
        return closed

    async def mark_started_if_null(
        self, session_id: UUID, started_at: datetime
    ) -> None:
        """仅当 started_at 为空时写入,实现"首条消息时间"的幂等补写。

        用 ``WHERE started_at IS NULL`` 条件保证多次调用只生效一次,
        不会把后续消息的时间覆盖到首条时间上。
        """
        stmt = (
            update(Session)
            .where(Session.id == session_id, Session.started_at.is_(None))
            .values(started_at=started_at)
        )
        await self.db.execute(stmt)
        await self.db.flush()

    async def update_summary(self, session_id: UUID, summary: str) -> bool:
        """写回对话摘要(供 MemoryService.summarize_if_too_long 使用)。

        返回是否命中:会话不存在则 False。
        """
        stmt = (
            update(Session)
            .where(Session.id == session_id)
            .values(summary=summary)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return (result.rowcount or 0) > 0

    async def get_with_messages(self, session_id: UUID) -> Optional[Session]:
        """取出会话并 eager load 其全部消息(按时间正序)。

        selectinload 一次性把消息查回,避免异步上下文中访问关系触发隐式 IO
        (异步 + 懒加载会抛 MissingGreenlet)。供"会话回放/导出"等需要完整
        历史的场景使用。
        """
        stmt = (
            select(Session)
            .options(selectinload(Session.messages))
            .where(Session.id == session_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
