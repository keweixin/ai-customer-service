"""审计日志 Repository:封装 audit_logs 表的数据访问。

对齐 API 层调用契约(auth.py / admin.py):
- ``AuditLogRepository(db).create(actor_id=, action=, detail=)`` -- 写审计
- ``AuditLogRepository(db).count_by_action_since(action_prefix=, since=)`` --
  管理后台"今日 LLM 调用量"统计(按 action 前缀 + 时间范围)
- ``AuditLogRepository(db).list_filtered(actor_id=, action=, limit=, offset=)``
  -- 审计日志列表(支持按操作者/动作前缀过滤)

任务要求的 list_by_user / list_by_action 作为 list_filtered 的特化保留。

字段映射:API 用 ``actor_id``(语义:操作发起者),模型列名为 ``user_id``
(技术:外键指向 users)。仓库层在 create 内做 ``actor_id -> user_id`` 映射,
对外暴露更贴切的 actor_id,对内复用模型列名,避免在模型上加 synonym 增加复杂度。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.audit_log import AuditLog
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class AuditLogRepository(BaseRepository[AuditLog]):
    """审计日志表数据访问对象(只追加,不更新不删除)。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(AuditLog, db)

    async def create(
        self,
        *,
        actor_id: Optional[UUID] = None,
        action: str,
        detail: Optional[dict[str, Any]] = None,
        target: Optional[str] = None,
        ip_address: Optional[str] = None,
        message: Optional[str] = None,
    ) -> AuditLog:
        """写一条审计日志。

        Args:
            actor_id: 操作发起者(用户 ID);系统操作传 None。
            action: 动作标识,点分字符串(如 ``user.login`` / ``llm.chat`` /
                    ``knowledge.upload``)。开放命名,由调用方约定规范。
            detail: 结构化详情(登录 IP / 工具入参 / 文档标题等)。
            target: 动作目标的可读标识(可选)。
            ip_address: 操作来源 IP(可选)。
            message: 结果摘要(可选,成功/失败原因)。
        """
        log = await super().create(
            {
                "user_id": actor_id,  # actor_id -> user_id 列映射
                "action": action,
                "detail": detail,
                "target": target,
                "ip_address": ip_address,
                "message": message,
            }  # type: ignore[arg-type]
        )
        _logger.info(
            "审计日志已写",
            audit_id=str(log.id),
            action=action,
            actor_id=str(actor_id) if actor_id else "system",
        )
        return log

    async def list_filtered(
        self,
        *,
        actor_id: Optional[UUID] = None,
        action: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        """分页查询审计日志,支持按操作者 + 动作(前缀)过滤。

        action 参数支持前缀匹配:传 ``"llm."`` 命中所有 llm.* 动作,
        传 ``"user.login"`` 精确匹配。前缀匹配契合管理后台"按事件类型筛查"。
        按 created_at 倒序(最新在前),契合运营排查习惯。
        """
        stmt = select(AuditLog)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.user_id == actor_id)
        if action:
            # 前缀匹配:action 以点结尾时按前缀,否则精确匹配。
            # 这样调用方既能传 "llm."(前缀)也能传 "user.login"(精确)。
            if action.endswith("."):
                stmt = stmt.where(AuditLog.action.like(f"{action}%"))
            else:
                stmt = stmt.where(AuditLog.action == action)
        stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_user(
        self, user_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[AuditLog]:
        """某用户的全部审计记录(list_filtered 的特化)。"""
        return await self.list_filtered(actor_id=user_id, limit=limit, offset=offset)

    async def list_by_action(
        self, action: str, *, limit: int = 50, offset: int = 0
    ) -> list[AuditLog]:
        """按动作(前缀)查询审计记录(list_filtered 的特化)。"""
        return await self.list_filtered(action=action, limit=limit, offset=offset)

    async def count_by_action_since(
        self, *, action_prefix: str, since: datetime
    ) -> int:
        """统计自 ``since`` 起,action 以 ``action_prefix`` 开头的记录数。

        管理后台"今日 LLM 调用量"使用:action_prefix="llm.", since=今日0点。
        """
        stmt = (
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.like(f"{action_prefix}%"))
            .where(AuditLog.created_at >= since)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())
