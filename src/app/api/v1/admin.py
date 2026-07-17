"""管理后台路由:统计看板 / 审计日志 / 用户列表。

设计要点:
- 全部 admin only,依赖 ``get_current_admin`` 链式校验(认证 + 角色)。
- 统计看板聚合多张表计数,在 repository 层用 SQL 聚合查询,避免 N+1;
  路由层只做编排与组装响应。
- 审计日志支持按 actor/action 过滤,便于按人/事件类型排查;
  分页防全量返回导致响应过大。
- 用户列表只返回非敏感字段(不含 password_hash),序列化由 schema 保证。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db
from app.core.logging import get_logger
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/stats",
    summary="统计看板",
)
async def stats(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """返回系统级统计数据(admin only)。

    指标:用户数 / 会话数 / 消息数 / 文档数 / 今日 LLM 调用量。
    今日调用量用于监控成本与异常突增,从 audit_log 或独立调用计数表取。
    """
    from datetime import datetime, timezone

    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.document_repository import DocumentRepository
    from app.repositories.message_repository import MessageRepository
    from app.repositories.session_repository import SessionRepository
    from app.repositories.user_repository import UserRepository
    from app.schemas.admin import StatsResponse

    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    msg_repo = MessageRepository(db)
    doc_repo = DocumentRepository(db)
    audit_repo = AuditLogRepository(db)

    # 各计数并行无依赖,但为简化用顺序调用(均在 DB 内聚合,毫秒级)。
    # 若需进一步优化,可用 asyncio.gather + 独立 session。
    user_count = await user_repo.count()
    session_count = await session_repo.count()
    message_count = await msg_repo.count()
    document_count = await doc_repo.count()

    # 今日 LLM 调用量:按 action 前缀 + 当日时间范围统计
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_calls = await audit_repo.count_by_action_since(
        action_prefix="llm.", since=today_start
    )

    logger.info("统计看板查询", admin_id=str(admin.id))
    return StatsResponse(
        users=user_count,
        sessions=session_count,
        messages=message_count,
        documents=document_count,
        today_llm_calls=today_calls,
    )  # type: ignore[call-arg]


@router.get(
    "/audit-logs",
    summary="审计日志列表",
)
async def list_audit_logs(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Query(default=None, description="按操作者过滤"),
    action: str | None = Query(default=None, description="按动作过滤(支持前缀如 user.)"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> None:
    """分页查询审计日志,支持按 actor/action 过滤(admin only)。

    按 created_at 倒序返回(最新在前),契合运营排查习惯。
    """
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.schemas.admin import AuditLogListResponse

    repo = AuditLogRepository(db)
    logs = await repo.list_filtered(
        actor_id=actor_id,
        action=action,
        limit=limit,
        offset=offset,
    )
    return AuditLogListResponse(logs=logs)  # type: ignore[call-arg]


@router.get(
    "/users",
    summary="用户列表",
)
async def list_users(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> None:
    """分页列出所有用户(admin only)。

    不返回 password_hash(schema 层排除),仅元信息与角色。
    """
    from app.repositories.user_repository import UserRepository
    from app.schemas.admin import UserListResponse

    repo = UserRepository(db)
    users = await repo.list_all(limit=limit, offset=offset)
    return UserListResponse(users=users)  # type: ignore[call-arg]
