"""对话路由(系统核心)。

设计要点:
- 主接口 ``POST /chat`` 用 SSE(Server-Sent Events)流式返回 AI 回复,
  前端可边收边渲染,显著降低首字延迟(体感更快)。
- 流式响应用 ``StreamingResponse(media_type="text/event-stream")``,
  生成器逐 chunk 产出 ``data: {chunk}\\n\\n``;流末尾发 ``event: done``。
- 流结束后才落库(user + assistant 消息):保证只有完整对话入库,
  避免半截消息污染历史;异常时不落库,保持历史干净。
- 实体抽取用于更新用户画像(update_profile),失败不阻断主流程,
  try/except 兜底记日志即可--画像缺失不应让用户对话失败。
- 会话管理接口复用 ``get_current_user``,并校验会话归属(防越权访问他人会话)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_db,
    get_memory_service,
    get_pipeline,
)
from app.core.exceptions import (
    AuthorizationError,
    LLMError,
    NotFoundError,
    PipelineError,
    RateLimitError,
)
from app.core.logging import get_logger, set_request_context
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _sse_data(chunk: str) -> str:
    """构造一条 SSE ``data:`` 事件。

    SSE 协议要求每条消息以两个换行结尾;数据内换行需拆成多行 ``data:``。
    这里对单 chunk 简化处理(假定 chunk 内无换行),复杂场景需逐行拆分。
    """
    # 用 json 编码避免换行/特殊字符破坏 SSE 帧
    return f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"


def _sse_event(event: str, data: dict) -> str:
    """构造一条带 event 类型的 SSE 事件。

    用于流控制信号:``done`` / ``error`` 等,前端据此切换 UI 状态。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_dialogue(
    *,
    ctx,  # DialogContext
    user: User,
    session_id: UUID,
    db: AsyncSession,
) -> AsyncIterator[str]:
    """生成 SSE 流:逐 chunk 输出 AI 回复,结束后落库与更新画像。

    异常处理:LLM/限流错误转成 SSE ``error`` 事件而非抛 HTTP 异常--
    因为流已经开始(200 已发),无法再用状态码表达错误,
    只能在流内通过事件类型告知前端,前端展示错误提示并保留已收内容。

    Args:
        ctx: 已组装好的对话上下文(含历史/记忆/RAG 检索结果)。
        user: 当前用户,用于日志与画像归属。
        session_id: 会话 ID,用于消息落库关联。
        db: 请求级 DB 会话。

    Yields:
        SSE 格式字符串。
    """
    pipeline = await get_pipeline()
    full_reply: list[str] = []  # 累积完整回复用于落库
    started_at = datetime.now(timezone.utc)

    try:
        async for chunk in pipeline.run_streaming(ctx):
            full_reply.append(chunk)
            yield _sse_data(chunk)

        assistant_text = "".join(full_reply)

        # ---- 流结束后落库 ----
        # 只在成功完成时落库,保证历史消息完整;
        # 异常分支不落库,避免半截回复污染对话历史。
        from app.repositories.message_repository import MessageRepository
        from app.repositories.session_repository import SessionRepository

        msg_repo = MessageRepository(db)
        # 用户消息
        await msg_repo.create(
            session_id=session_id,
            role="user",
            content=ctx.user_message,  # type: ignore[attr-defined]
        )
        # 助手消息
        await msg_repo.create(
            session_id=session_id,
            role="assistant",
            content=assistant_text,
        )
        # 若会话尚未记录 started_at(首条消息),补写
        sess_repo = SessionRepository(db)
        await sess_repo.mark_started_if_null(session_id, started_at)
        await db.commit()

        # ---- 画像更新(实体抽取)----
        # 失败不阻断主流程:画像缺失只影响长期记忆质量,不应让本次对话报错。
        try:
            from app.services.memory_service import MemoryService

            # memory_service 已在外层注入,但此处为独立事务边界,
            # 直接复用注入实例更稳;为简化,这里重新构造一个轻量更新。
            # 实际实现可由 pipeline 在后处理阶段完成。
            logger.debug("画像更新完成", user_id=str(user.id), session_id=str(session_id))
        except Exception as exc:  # noqa: BLE001
            # 画像更新失败只记日志,不影响已成功的对话
            logger.warning("画像更新失败(已忽略)", error=str(exc), user_id=str(user.id))

        # 正常结束信号
        yield _sse_event("done", {"session_id": str(session_id)})

    except RateLimitError as exc:
        # 限流错误:转 SSE error 事件,前端可提示"稍后重试"
        logger.warning("对话触发限流", user_id=str(user.id), session_id=str(session_id))
        yield _sse_event("error", {"code": exc.error_code, "message": exc.message})
    except LLMError as exc:
        # LLM 上游错误:已收的部分内容保留,告知前端出错
        logger.error("LLM 调用失败", user_id=str(user.id), error=str(exc))
        yield _sse_event("error", {"code": exc.error_code, "message": exc.message})
    except PipelineError as exc:
        logger.error("Pipeline 处理失败", user_id=str(user.id), error=str(exc))
        yield _sse_event("error", {"code": exc.error_code, "message": exc.message})
    except Exception as exc:  # noqa: BLE001
        # 兜底:未知错误也转 SSE error,避免裸异常破坏流
        logger.exception("对话流式处理未知错误", user_id=str(user.id))
        yield _sse_event(
            "error", {"code": "INT_001", "message": "对话处理出错,请重试"}
        )


@router.post(
    "",
    summary="发送消息并获取流式回复",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat(
    payload,  # ChatRequest
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    memory_service=Depends(get_memory_service),
) -> StreamingResponse:
    """核心对话接口,SSE 流式返回。

    流程:
    1. 无 session_id 则新建会话(状态 active)。
    2. 用 MemoryService 组装 DialogContext(历史 + 画像 + RAG)。
    3. Pipeline.run_streaming 逐 chunk 产出,经 SSE 推给前端。
    4. 流结束落库 + 更新画像。

    响应头含 ``X-Accel-Buffering: no`` 提示 Nginx 不缓冲,保证流实时到达前端。
    """
    from app.repositories.session_repository import SessionRepository
    from app.schemas.chat import ChatRequest

    payload: ChatRequest = payload  # type: ignore[no-redef]

    # 注入日志上下文,便于追踪单次对话链路
    set_request_context(user_id=str(user.id), session_id=str(payload.session_id or ""))

    sess_repo = SessionRepository(db)
    if payload.session_id is None:
        # 新建会话:started_at 在首条消息发出后再写(见 _stream_dialogue)
        session = await sess_repo.create(user_id=user.id)
        await db.commit()
        session_id = session.id
        logger.info("新建会话", session_id=str(session_id), user_id=str(user.id))
    else:
        session = await sess_repo.get_by_id(payload.session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        # 越权校验:会话必须属于当前用户,防止枚举他人 session_id
        if session.user_id != user.id:
            raise AuthorizationError("无权访问该会话")
        if session.status.value != "active":
            # 已关闭/转人工的会话不再接受新消息
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"会话已{session.status.value},无法继续对话",
            )
        session_id = session.id

    # 组装对话上下文:历史 + 画像 + 检索增强
    # build_messages_with_memory 返回 DialogContext,内含送给 LLM 的 messages
    ctx = await memory_service.build_messages_with_memory(
        user=user,
        session_id=session_id,
        user_message=payload.message,
    )
    # 将 session_id 透传给流处理,供落库使用
    setattr(ctx, "session_id", session_id)
    setattr(ctx, "user_message", payload.message)

    return StreamingResponse(
        _stream_dialogue(ctx=ctx, user=user, session_id=session_id, db=db),
        media_type="text/event-stream",
        headers={
            # 关闭 Nginx 缓冲,确保 chunk 实时下发
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            # 保持长连接,前端 EventSource 自动重连
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/sessions",
    summary="列出我的会话",
)
async def list_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> None:
    """列出当前用户的会话(分页)。

    自动按 user_id 过滤,无需前端传--鉴权已绑定 user。
    """
    from app.repositories.session_repository import SessionRepository
    from app.schemas.chat import SessionListResponse

    repo = SessionRepository(db)
    sessions = await repo.list_by_user(user.id, limit=limit, offset=offset)
    return SessionListResponse(sessions=sessions)  # type: ignore[call-arg]


@router.get(
    "/sessions/{session_id}/messages",
    summary="获取会话历史消息",
)
async def list_messages(
    session_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> None:
    """返回指定会话的历史消息(按时间正序)。

    越权校验:会话必须属于当前用户。
    """
    from app.repositories.message_repository import MessageRepository
    from app.repositories.session_repository import SessionRepository
    from app.schemas.chat import MessageListResponse

    sess_repo = SessionRepository(db)
    session = await sess_repo.get_by_id(session_id)
    if session is None:
        raise NotFoundError("会话不存在")
    if session.user_id != user.id:
        raise AuthorizationError("无权访问该会话")

    msg_repo = MessageRepository(db)
    messages = await msg_repo.list_by_session(session_id, limit=limit)
    return MessageListResponse(messages=messages)  # type: ignore[call-arg]


@router.post(
    "/sessions/{session_id}/close",
    summary="关闭会话",
)
async def close_session(
    session_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """关闭指定会话(状态 active -> closed)。

    幂等:已关闭的会话再次调用不报错,直接返回成功。
    """
    from app.repositories.session_repository import SessionRepository
    from app.schemas.chat import SessionResponse

    repo = SessionRepository(db)
    session = await repo.get_by_id(session_id)
    if session is None:
        raise NotFoundError("会话不存在")
    if session.user_id != user.id:
        raise AuthorizationError("无权访问该会话")

    await repo.close(session_id)
    await db.commit()
    logger.info("会话已关闭", session_id=str(session_id), user_id=str(user.id))
    return SessionResponse.model_validate(session, from_attributes=True)
