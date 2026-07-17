"""长期记忆服务:用户画像 + 对话历史管理。

业务核心能力之二:跨会话的长期记忆,避免每轮都把全部历史塞进 prompt,
降低 token 成本并保持上下文连贯。

职责:
- 用户画像:get_or_create_profile / update_profile(JSONB merge)/ get_profile_summary;
- 对话历史:save_message / get_recent_messages(滑动窗口);
- 上下文组装:build_messages_with_memory -- 拼装 system(含画像)+ 历史 + 当前输入,
  返回 DialogContext 供 Pipeline 消费;
- 摘要压缩:summarize_if_too_long -- 消息超阈值时用 LLM 把更早的摘要成一段,
  存到 session.summary,并删除已压缩消息。

对齐 API 层契约(chat.py / deps.py):
- ``MemoryService(db=db, llm=llm)``
- ``await mem.build_messages_with_memory(user=, session_id=, user_message=)``
  -> 返回 DialogContext(pipeline.run_streaming 的输入)

注意:
- build_messages_with_memory 返回 DialogContext(非裸 list),因为 chat.py
  直接把它喂给 pipeline.run_streaming(ctx),且后续 setattr session_id/user_message;
- system prompt 注入画像摘要与 session.summary(历史压缩),两者叠加构成
  完整长期记忆;
- 历史消息从 get_recent_messages 取最近 N 条(滑动窗口),按正序拼装。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DatabaseError
from app.core.logging import get_logger
from app.models.user_profile import UserProfile
from app.pipeline.context import DialogContext
from app.repositories.message_repository import MessageRepository
from app.repositories.profile_repository import UserProfileRepository
from app.repositories.session_repository import SessionRepository

_logger = get_logger(__name__)

# 滑动窗口默认大小:最近 N 轮历史注入 prompt。
# 20 是经验值:兼顾上下文连贯与 token 成本;可由 summarize_if_too_long 配合压缩。
DEFAULT_HISTORY_LIMIT = 20


class MemoryService:
    """长期记忆服务:画像 + 历史 + 上下文组装。

    Args:
        db: 请求级数据库会话。
        llm: LLMService 实例(供 summarize 调用)。
    """

    def __init__(self, db: AsyncSession, llm: Any) -> None:
        self.db = db
        self.llm = llm
        self._profile_repo = UserProfileRepository(db)
        self._message_repo = MessageRepository(db)
        self._session_repo = SessionRepository(db)

    # ------------------------------------------------------------------
    # 用户画像
    # ------------------------------------------------------------------
    async def get_or_create_profile(self, user_id: UUID) -> UserProfile:
        """取用户画像,不存在则建空画像(一对一)。

        用 upsert 保证幂等:并发首次访问不会重复创建。
        """
        profile = await self._profile_repo.get_by_user(user_id)
        if profile is not None:
            return profile
        # 不存在 -> upsert 空画像(profile_data={}, summary=None)
        return await self._profile_repo.upsert(user_id=user_id, profile_data={})

    async def update_profile(self, user_id: UUID, entities: dict[str, Any]) -> None:
        """把新实体 merge 进画像 profile_data(JSONB ``||`` 操作符)。

        单条 SQL 原子合并,顶层键覆盖;失败不抛中断主流程(由调用方 try/except)。
        """
        if not entities:
            return
        await self._profile_repo.append_entities(user_id, entities)
        _logger.info("画像已更新", user_id=str(user_id), keys=list(entities.keys()))

    async def get_profile_summary(self, user_id: UUID) -> str:
        """返回画像的自然语言摘要,用于注入 system prompt。

        优先用 profile.summary(LLM 生成的摘要);为空则现场用 profile_data
        拼装可读文本(不调 LLM,避免每次对话都产生摘要调用)。
        """
        profile = await self.get_or_create_profile(user_id)
        if profile.summary:
            return profile.summary
        # 现场拼装:把 profile_data 转成可读键值列表
        data = profile.profile_data or {}
        if not data:
            return ""
        lines = [f"- {k}: {v}" for k, v in data.items() if v is not None]
        if not lines:
            return ""
        return "已知用户信息:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # 对话历史
    # ------------------------------------------------------------------
    async def save_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        *,
        tokens: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Any:
        """写一条消息到 messages 表。

        role 为字符串("user"/"assistant"/"system"/"tool"),仓库层转枚举。
        """
        return await self._message_repo.create(
            session_id=session_id,
            role=role,
            content=content,
            tokens_used=tokens,
            metadata=metadata,
        )

    async def get_recent_messages(
        self, session_id: UUID, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """取最近 N 条消息,按时间正序返回(滑动窗口)。

        返回 OpenAI 兼容的 ``{role, content}`` 结构,可直接拼进 LLM messages。
        """
        messages = await self._message_repo.get_recent(session_id, limit=limit)
        return [{"role": m.role.value, "content": m.content} for m in messages]

    # ------------------------------------------------------------------
    # 上下文组装(核心)
    # ------------------------------------------------------------------
    async def build_messages_with_memory(
        self,
        *,
        user: Any,
        session_id: UUID,
        user_message: str,
        system_prompt: Optional[str] = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> DialogContext:
        """组装完整对话上下文,返回 DialogContext 供 Pipeline 消费。

        组装顺序(影响 LLM 注意力,system 在前最稳):
        1. system 消息:基础 prompt + 画像摘要 + session.summary(历史压缩);
        2. 最近 N 轮历史消息(滑动窗口,正序);
        3. 当前用户输入(作为最新 user 消息)。

        Args:
            user: 当前用户对象(取 .id);也可直接传 user_id。
            session_id: 会话 ID,取历史与 summary。
            user_message: 本轮用户输入。
            system_prompt: 基础系统提示词;None 用默认客服人设。
            history_limit: 历史消息条数上限。

        Returns:
            DialogContext,messages 字段已填好,session_id/user_id/user_input 已设。
        """
        # user 兼容 User 对象与 UUID
        user_id: UUID = user.id if hasattr(user, "id") else user  # type: ignore[assignment]

        # 1. 画像摘要(长期记忆)
        profile_summary = await self.get_profile_summary(user_id)

        # 2. session.summary(历史压缩记忆)
        session = await self._session_repo.get_by_id(session_id)
        session_summary = session.summary if session else None

        # 3. 组装 system prompt
        base_prompt = system_prompt or (
            "你是一名专业、礼貌、高效的 AI 客服。请基于知识库与用户历史"
            "准确回答问题;不确定时如实说明并引导转人工。回答简洁,避免编造。"
        )
        system_parts = [base_prompt]
        if profile_summary:
            system_parts.append(f"\n\n[用户画像]\n{profile_summary}")
        if session_summary:
            system_parts.append(f"\n\n[对话历史摘要]\n{session_summary}")
        system_content = "".join(system_parts)

        # 4. 取最近历史
        history = await self.get_recent_messages(session_id, limit=history_limit)

        # 5. 拼 messages
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # 6. 构造 DialogContext
        ctx = DialogContext(
            session_id=str(session_id),
            user_id=str(user_id),
            user_input=user_message,
            cleaned_input=user_message,
            messages=messages,
        )
        _logger.info(
            "上下文组装完成",
            session_id=str(session_id),
            user_id=str(user_id),
            history_count=len(history),
            has_profile=bool(profile_summary),
            has_summary=bool(session_summary),
        )
        return ctx

    # ------------------------------------------------------------------
    # 摘要压缩
    # ------------------------------------------------------------------
    async def summarize_if_too_long(
        self,
        session_id: UUID,
        max_messages: int = DEFAULT_HISTORY_LIMIT,
    ) -> Optional[str]:
        """消息超过 max_messages 时,把更早的消息摘要成一段存到 session.summary。

        策略:
        1. 统计当前消息数;<= max_messages 直接返回(None 表示未触发);
        2. 取全部消息,保留最近 max_messages 条不动;
        3. 把更早的消息拼成文本,调 LLM.summarize 压缩;
        4. 若 session 已有 summary,新摘要与旧摘要合并(避免历史摘要丢失);
        5. 写回 session.summary,并删除已摘要的早期消息(delete_older_than)。

        Args:
            session_id: 目标会话。
            max_messages: 保留的最近消息条数;超过则触发摘要。

        Returns:
            新摘要文本;未触发返回 None。
        """
        total = await self._message_repo.count_by_session(session_id)
        if total <= max_messages:
            return None

        # 取全部消息正序,前 (total - max_messages) 条待摘要
        all_messages = await self._message_repo.list_by_session(
            session_id, limit=total
        )
        to_summarize = all_messages[: total - max_messages]
        if not to_summarize:
            return None

        # 拼成可读对话文本
        dialogue_lines = [f"{m.role.value}: {m.content}" for m in to_summarize]
        dialogue_text = "\n".join(dialogue_lines)

        # 已有 summary 作为前置上下文一起压缩(保留累积记忆)
        session = await self._session_repo.get_by_id(session_id)
        existing_summary = session.summary if session else None
        if existing_summary:
            dialogue_text = f"[已有历史摘要]\n{existing_summary}\n\n[新增对话]\n{dialogue_text}"

        # 调 LLM 摘要
        try:
            new_summary = await self.llm.summarize(
                dialogue_text,
                instruction=(
                    "你是客服对话摘要助手。请把以下客服对话历史(含已有摘要)压缩成"
                    "一段简洁摘要,保留:用户诉求、已确认的信息、已提供的方案、"
                    "待办与未决事项。不要编造,不要罗列寒暄。"
                ),
            )
        except Exception as exc:
            _logger.error(
                "摘要生成失败(已跳过压缩)",
                session_id=str(session_id),
                error=str(exc),
            )
            return None

        # 写回 session.summary
        updated = await self._session_repo.update_summary(session_id, new_summary)
        if not updated:
            raise DatabaseError(
                "摘要写回失败:会话不存在", detail={"session_id": str(session_id)}
            )

        # 删除已摘要的早期消息(保留最近 max_messages 条)
        await self._message_repo.delete_older_than(session_id, keep_count=max_messages)

        _logger.info(
            "历史已摘要压缩",
            session_id=str(session_id),
            summarized_count=len(to_summarize),
            kept=max_messages,
            summary_len=len(new_summary),
        )
        return new_summary
