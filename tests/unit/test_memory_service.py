"""记忆服务单元测试(mock db)。

被测模块: ``app.services.memory_service``(MemoryService)

长期记忆服务:用户画像 + 对话历史 + 上下文组装。实际契约:
- ``MemoryService(db, llm)`` 构造,内部建 UserProfileRepository /
  MessageRepository / SessionRepository。
- ``get_or_create_profile(user_id) -> UserProfile``:取画像,不存在则建空画像。
- ``update_profile(user_id, entities) -> None``:merge 实体进 profile_data。
- ``get_profile_summary(user_id) -> str``:返回画像自然语言摘要。
- ``get_recent_messages(session_id, limit) -> list[dict]``:取最近 N 条历史。
- ``build_messages_with_memory(*, user, session_id, user_message, ...) -> DialogContext``。
- ``summarize_if_too_long(session_id, max_messages)``:超长时压缩历史。

DB 通过 mock 仓库隔离,不写真实库。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.memory_service import MemoryService


def _make_memory_service(
    profile_repo: MagicMock | None = None,
    message_repo: MagicMock | None = None,
    session_repo: MagicMock | None = None,
    llm: MagicMock | None = None,
) -> MemoryService:
    """构造 MemoryService,注入 mock 仓库 / llm。

    构造期会用 db 建 3 个仓库,这里通过先构造再替换仓库属性的方式注入 mock,
    避免依赖真实仓库实现。
    """
    db = MagicMock()
    if llm is None:
        llm = MagicMock()
        llm.summarize = AsyncMock(return_value="对话摘要")
    svc = MemoryService(db=db, llm=llm)
    if profile_repo is not None:
        svc._profile_repo = profile_repo
    if message_repo is not None:
        svc._message_repo = message_repo
    if session_repo is not None:
        svc._session_repo = session_repo
    return svc


def _mock_profile(summary: str | None = None, data: dict | None = None) -> MagicMock:
    """构造一个 mock UserProfile。"""
    p = MagicMock()
    p.id = uuid.uuid4()
    p.user_id = uuid.uuid4()
    p.summary = summary
    p.profile_data = data or {}
    return p


class TestGetOrCreateProfile:
    """get_or_create_profile 行为。"""

    @pytest.mark.asyncio
    async def test_get_or_create_profile_returns_existing(self) -> None:
        """已有画像时应返回现有画像,不新建。"""
        existing = _mock_profile(summary="x", data={"name": "张三"})
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=existing)
        profile_repo.upsert = AsyncMock(return_value=existing)
        svc = _make_memory_service(profile_repo=profile_repo)

        result = await svc.get_or_create_profile(uuid.uuid4())

        assert result is existing
        profile_repo.upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_or_create_profile_creates_when_missing(self) -> None:
        """无画像时应 upsert 创建空画像并返回。"""
        new_profile = _mock_profile()
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=None)
        profile_repo.upsert = AsyncMock(return_value=new_profile)
        svc = _make_memory_service(profile_repo=profile_repo)

        result = await svc.get_or_create_profile(uuid.uuid4())

        assert result is new_profile
        profile_repo.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_profile_idempotent(self) -> None:
        """同一 user_id 多次调用,第二次应命中已有画像,不重复 upsert。"""
        existing = _mock_profile()
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=existing)
        profile_repo.upsert = AsyncMock(return_value=existing)
        svc = _make_memory_service(profile_repo=profile_repo)

        uid = uuid.uuid4()
        await svc.get_or_create_profile(uid)
        await svc.get_or_create_profile(uid)

        profile_repo.upsert.assert_not_awaited()


class TestUpdateProfile:
    """update_profile 实体合并。"""

    @pytest.mark.asyncio
    async def test_update_profile_calls_append_entities(self) -> None:
        """update_profile 应调 profile_repo.append_entities 做原子合并。"""
        profile_repo = MagicMock()
        profile_repo.append_entities = AsyncMock(return_value=None)
        svc = _make_memory_service(profile_repo=profile_repo)

        uid = uuid.uuid4()
        await svc.update_profile(uid, {"city": "北京"})

        profile_repo.append_entities.assert_awaited_once()
        call_args = profile_repo.append_entities.call_args
        assert call_args.args[0] == uid
        assert call_args.args[1] == {"city": "北京"}

    @pytest.mark.asyncio
    async def test_update_profile_empty_entities_noop(self) -> None:
        """空 entities 字典应直接返回,不调仓库。"""
        profile_repo = MagicMock()
        profile_repo.append_entities = AsyncMock()
        svc = _make_memory_service(profile_repo=profile_repo)

        await svc.update_profile(uuid.uuid4(), {})
        profile_repo.append_entities.assert_not_awaited()


class TestGetProfileSummary:
    """get_profile_summary 摘要生成。"""

    @pytest.mark.asyncio
    async def test_summary_uses_stored_summary_when_present(self) -> None:
        """profile.summary 非空时应直接返回它,不现场拼装。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(
            return_value=_mock_profile(summary="用户偏好简洁", data={"x": 1})
        )
        svc = _make_memory_service(profile_repo=profile_repo)

        summary = await svc.get_profile_summary(uuid.uuid4())
        assert summary == "用户偏好简洁"

    @pytest.mark.asyncio
    async def test_summary_built_from_profile_data_when_no_summary(self) -> None:
        """无 summary 但有 profile_data 时,应现场拼装可读文本。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(
            return_value=_mock_profile(summary=None, data={"name": "张三", "city": "北京"})
        )
        svc = _make_memory_service(profile_repo=profile_repo)

        summary = await svc.get_profile_summary(uuid.uuid4())
        assert isinstance(summary, str)
        assert "张三" in summary
        assert "北京" in summary

    @pytest.mark.asyncio
    async def test_summary_empty_when_no_data(self) -> None:
        """无 summary 且 profile_data 为空时,应返回空字符串。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(
            return_value=_mock_profile(summary=None, data={})
        )
        svc = _make_memory_service(profile_repo=profile_repo)

        summary = await svc.get_profile_summary(uuid.uuid4())
        assert summary == ""


class TestGetRecentMessages:
    """get_recent_messages 限量读取历史。"""

    @pytest.mark.asyncio
    async def test_get_recent_messages_limit(self) -> None:
        """get_recent_messages 应只返回最近 limit 条,正序。"""
        msgs = [
            MagicMock(role=MagicMock(value="user"), content="第一条"),
            MagicMock(role=MagicMock(value="assistant"), content="第二条"),
        ]
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=msgs)
        svc = _make_memory_service(message_repo=message_repo)

        result = await svc.get_recent_messages(uuid.uuid4(), limit=5)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "第一条"}
        assert result[1] == {"role": "assistant", "content": "第二条"}

    @pytest.mark.asyncio
    async def test_get_recent_messages_empty_session(self) -> None:
        """新会话(无历史)应返回空列表。"""
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        svc = _make_memory_service(message_repo=message_repo)

        result = await svc.get_recent_messages(uuid.uuid4(), limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_recent_messages_passes_limit_to_repo(self) -> None:
        """limit 应透传给仓库。"""
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        svc = _make_memory_service(message_repo=message_repo)

        await svc.get_recent_messages(uuid.uuid4(), limit=7)
        call_kwargs = message_repo.get_recent.call_args.kwargs
        assert call_kwargs.get("limit") == 7


class TestBuildMessagesWithMemory:
    """build_messages_with_memory 组装上下文。"""

    @pytest.mark.asyncio
    async def test_build_messages_returns_dialog_context(self) -> None:
        """应返回 DialogContext,messages 已填好。"""
        from app.pipeline.context import DialogContext

        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=_mock_profile(summary=None, data={}))
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        session_repo = MagicMock()
        session_repo.get_by_id = AsyncMock(return_value=None)
        svc = _make_memory_service(
            profile_repo=profile_repo,
            message_repo=message_repo,
            session_repo=session_repo,
        )

        user = MagicMock(id=uuid.uuid4())
        ctx = await svc.build_messages_with_memory(
            user=user, session_id=uuid.uuid4(), user_message="你好"
        )

        assert isinstance(ctx, DialogContext)
        assert ctx.user_input == "你好"
        assert len(ctx.messages) >= 1

    @pytest.mark.asyncio
    async def test_build_messages_has_system_first(self) -> None:
        """messages 首条应为 system 消息。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=_mock_profile(summary=None, data={}))
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        session_repo = MagicMock()
        session_repo.get_by_id = AsyncMock(return_value=None)
        svc = _make_memory_service(
            profile_repo=profile_repo,
            message_repo=message_repo,
            session_repo=session_repo,
        )

        ctx = await svc.build_messages_with_memory(
            user=MagicMock(id=uuid.uuid4()),
            session_id=uuid.uuid4(),
            user_message="你好",
        )
        assert ctx.messages[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_build_messages_ends_with_current_user_message(self) -> None:
        """messages 末尾应为当前用户输入。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=_mock_profile(summary=None, data={}))
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        session_repo = MagicMock()
        session_repo.get_by_id = AsyncMock(return_value=None)
        svc = _make_memory_service(
            profile_repo=profile_repo,
            message_repo=message_repo,
            session_repo=session_repo,
        )

        ctx = await svc.build_messages_with_memory(
            user=MagicMock(id=uuid.uuid4()),
            session_id=uuid.uuid4(),
            user_message="查我的订单",
        )
        assert ctx.messages[-1]["role"] == "user"
        assert ctx.messages[-1]["content"] == "查我的订单"

    @pytest.mark.asyncio
    async def test_build_messages_includes_history(self) -> None:
        """历史消息应插在 system 与当前 user 消息之间。"""
        history_msgs = [
            MagicMock(role=MagicMock(value="user"), content="旧问题"),
            MagicMock(role=MagicMock(value="assistant"), content="旧回答"),
        ]
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(return_value=_mock_profile(summary=None, data={}))
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=history_msgs)
        session_repo = MagicMock()
        session_repo.get_by_id = AsyncMock(return_value=None)
        svc = _make_memory_service(
            profile_repo=profile_repo,
            message_repo=message_repo,
            session_repo=session_repo,
        )

        ctx = await svc.build_messages_with_memory(
            user=MagicMock(id=uuid.uuid4()),
            session_id=uuid.uuid4(),
            user_message="新问题",
        )
        roles = [m["role"] for m in ctx.messages]
        # 顺序:system, user(历史), assistant(历史), user(当前)
        assert roles == ["system", "user", "assistant", "user"]

    @pytest.mark.asyncio
    async def test_build_messages_includes_profile_summary(self) -> None:
        """有画像摘要时,应注入 system 消息。"""
        profile_repo = MagicMock()
        profile_repo.get_by_user = AsyncMock(
            return_value=_mock_profile(summary="用户偏好简洁回复", data={})
        )
        message_repo = MagicMock()
        message_repo.get_recent = AsyncMock(return_value=[])
        session_repo = MagicMock()
        session_repo.get_by_id = AsyncMock(return_value=None)
        svc = _make_memory_service(
            profile_repo=profile_repo,
            message_repo=message_repo,
            session_repo=session_repo,
        )

        ctx = await svc.build_messages_with_memory(
            user=MagicMock(id=uuid.uuid4()),
            session_id=uuid.uuid4(),
            user_message="你好",
        )
        system_content = ctx.messages[0]["content"]
        assert "用户偏好简洁回复" in system_content
