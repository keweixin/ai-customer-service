"""用户画像 Repository:封装 user_profiles 表的数据访问。

长期记忆的持久化层:存跨会话的实体画像(JSONB)+ 对话摘要(Text)。

对齐 MemoryService 需要:
- ``get_by_user(user_id)`` -- 取画像(不存在返回 None)
- ``upsert(user_id, profile_data=, summary=)`` -- 用 PostgreSQL
  ``ON CONFLICT (user_id) DO UPDATE`` 单条 SQL 完成插入或更新,
  避免读-改-写竞态(并发更新同一画像不互相覆盖)。
- ``append_entities(user_id, entities)`` -- 用 JSONB ``||`` 操作符把新实体
  merge 进现有 profile_data,单条 SQL 原子合并(顶层键覆盖,深层键需业务自行结构化)。

JSONB merge 说明:
- ``profile_data || new``:顶层键合并,新值覆盖旧值;
- 深层 merge 需用 ``jsonb_strip_nulls(jsonb_set(...))`` 等组合,本层不预设,
  交给调用方按业务结构组织 entities(如 {"preferences": {...}} 整体替换)。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.user_profile import UserProfile
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class UserProfileRepository(BaseRepository[UserProfile]):
    """用户画像表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(UserProfile, db)

    async def get_by_user(self, user_id: UUID) -> Optional[UserProfile]:
        """按 user_id 取画像(一对一)。"""
        stmt = select(UserProfile).where(UserProfile.user_id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        user_id: UUID,
        profile_data: Optional[dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> UserProfile:
        """插入或更新画像(ON CONFLICT 单条 SQL)。

        语义:
        - 不存在 -> INSERT;
        - 已存在 -> UPDATE:profile_data 用传入值整体替换(NULL 则不动),
          summary 同理。整字段级覆盖,不做深层 merge(深层 merge 见 append_entities)。

        返回 upsert 后的画像行(select 回读,拿到 server_default 字段)。
        """
        # 构造 values:必填 user_id;profile_data / summary 仅在非 None 时写入,
        # 用 do_update 的 set 动态构造,避免把 NULL 覆盖到已有值上。
        values: dict[str, Any] = {"user_id": user_id}
        set_on_update: dict[str, Any] = {}
        if profile_data is not None:
            values["profile_data"] = profile_data
            set_on_update["profile_data"] = profile_data
        if summary is not None:
            values["summary"] = summary
            set_on_update["summary"] = summary

        stmt = pg_insert(UserProfile).values(**values)
        if set_on_update:
            # ON CONFLICT (user_id) DO UPDATE:存在则更新指定字段。
            stmt = stmt.on_conflict_do_update(
                index_elements=[UserProfile.user_id],
                set_=set_on_update,
            )
        else:
            # 无字段可更新时,DO NOTHING(仅保证行存在)。
            stmt = stmt.on_conflict_do_nothing(index_elements=[UserProfile.user_id])

        await self.db.execute(stmt)
        await self.db.flush()

        # 回读拿到完整行(server_default 的 id/created_at 等)
        profile = await self.get_by_user(user_id)
        assert profile is not None  # upsert 后必定存在
        return profile

    async def append_entities(
        self, user_id: UUID, entities: dict[str, Any]
    ) -> Optional[UserProfile]:
        """把新实体 merge 进 profile_data(JSONB ``||`` 操作符)。

        用 PostgreSQL 原生 JSONB 合并操作符,单条 SQL 原子完成:
        ``profile_data = profile_data || :new_entities``
        顶层键合并(新覆盖旧),避免应用层读-改-写竞态。

        若画像行不存在,先 upsert 一个空画像再合并,保证幂等。
        """
        # 确保行存在(避免对不存在的行做 || 报错)
        existing = await self.get_by_user(user_id)
        if existing is None:
            await self.upsert(user_id=user_id, profile_data=entities)
            return await self.get_by_user(user_id)

        # 用原生 SQL 触发 JSONB || 操作符(SQLAlchemy 表达式层对 || 的支持需
        # 借助 cast,直接用 text 更直观且可控)。
        from sqlalchemy import text

        stmt = text(
            "UPDATE user_profiles SET profile_data = profile_data || :entities "
            "WHERE user_id = :uid"
        )
        await self.db.execute(
            stmt,
            {"entities": entities, "uid": user_id},
        )
        await self.db.flush()
        _logger.info(
            "画像实体已合并",
            user_id=str(user_id),
            keys=list(entities.keys()),
        )
        # refresh 现有对象使其 profile_data 反映最新合并结果
        await self.db.refresh(existing)
        return existing

    async def update_summary(self, user_id: UUID, summary: str) -> bool:
        """更新画像的对话摘要(可选能力,MemoryService 也可存到 session)。"""
        stmt = (
            update(UserProfile)
            .where(UserProfile.user_id == user_id)
            .values(summary=summary)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return (result.rowcount or 0) > 0
