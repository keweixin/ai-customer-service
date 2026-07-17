"""内容安全阶段单元测试(mock LLM)。

被测模块: ``app.pipeline.stages.content_guard``(ContentGuard)

阶段 2:用 LLM 判断用户输入是否安全,不安全则短路。实际实现是 ``BaseStage`` 子类,
``async run(ctx)`` 调 ``llm.chat(messages)``(返回 ``{"content": "..."}``),
解析 LLM 输出的 JSON ``{"is_safe": bool, "reason": str}``,写回
``ctx.is_safe`` / ``ctx.short_circuit`` / ``ctx.short_circuit_reply``。

容错策略:LLM 异常或解析失败时**默认放行**(is_safe=True),避免抖动阻断所有用户。
LLM 全程 mock,不发真实请求。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pipeline.context import DialogContext
from app.pipeline.stages.content_guard import ContentGuard


def _llm_returning(content: str) -> MagicMock:
    """构造一个 mock LLM,其 chat() 返回 {"content": content}。"""
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"content": content, "tool_calls": [], "usage": {}})
    return llm


def _ctx_with_input(text: str) -> DialogContext:
    """构造一个已清洗输入就绪的 context。"""
    ctx = DialogContext(user_input=text, cleaned_input=text)
    return ctx


class TestSafeContent:
    """安全内容应放行。"""

    @pytest.mark.asyncio
    async def test_safe_content_passes(self) -> None:
        """LLM 返回 is_safe=True 时,ctx.is_safe 应为 True 且不短路。"""
        llm = _llm_returning(json.dumps({"is_safe": True, "reason": ""}))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("你好,我想查一下我的订单")

        await guard.run(ctx)

        assert ctx.is_safe is True
        assert ctx.short_circuit is False
        llm.chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_normal_question_passes(self) -> None:
        """正常业务咨询应放行。"""
        llm = _llm_returning(json.dumps({"is_safe": True, "reason": ""}))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("退货流程是怎样的?")

        await guard.run(ctx)
        assert ctx.is_safe is True


class TestUnsafeContent:
    """违规内容应短路。"""

    @pytest.mark.asyncio
    async def test_unsafe_content_short_circuits(self) -> None:
        """LLM 返回 is_safe=False 时,应短路并填 short_circuit_reply。"""
        llm = _llm_returning(
            json.dumps({"is_safe": False, "reason": "包含辱骂内容"})
        )
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("一些违规文字内容")

        await guard.run(ctx)

        assert ctx.is_safe is False
        assert ctx.short_circuit is True
        assert isinstance(ctx.short_circuit_reply, str)
        assert len(ctx.short_circuit_reply) > 0, "短路回复不应为空"

    @pytest.mark.asyncio
    async def test_unsafe_short_circuit_reply_is_fixed(self) -> None:
        """短路回复应是固定拒答文案(不依赖 LLM 生成,可控可审计)。"""
        llm = _llm_returning(json.dumps({"is_safe": False, "reason": "敏感"}))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("敏感内容")

        await guard.run(ctx)
        # 固定文案应含"抱歉"或"转人工"类提示
        assert "抱歉" in ctx.short_circuit_reply or "人工" in ctx.short_circuit_reply

    @pytest.mark.asyncio
    async def test_unsafe_does_not_call_downstream(self) -> None:
        """短路后 ctx.short_circuit=True,Runner 据此跳过后续阶段(此处验证标志)。"""
        llm = _llm_returning(json.dumps({"is_safe": False, "reason": "广告"}))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("加我微信买货")

        await guard.run(ctx)
        assert ctx.short_circuit is True, "不安全应触发 short_circuit 标志"


class TestLLMErrorHandling:
    """LLM 异常 / 非法返回时的容错(默认放行)。"""

    @pytest.mark.asyncio
    async def test_llm_error_defaults_to_safe(self) -> None:
        """LLM 抛异常时,应默认放行(is_safe=True),不阻断用户。"""
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("LLM 超时"))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("任意输入")

        await guard.run(ctx)
        assert ctx.is_safe is True
        assert ctx.short_circuit is False

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_json_defaults_safe(self) -> None:
        """LLM 返回无法解析的 JSON 时,应默认放行。"""
        llm = _llm_returning("这不是 JSON")
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("任意输入")

        await guard.run(ctx)
        assert ctx.is_safe is True

    @pytest.mark.asyncio
    async def test_llm_returns_none_content_defaults_safe(self) -> None:
        """LLM 返回空 content 时,应默认放行。"""
        llm = _llm_returning("")
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("任意输入")

        await guard.run(ctx)
        assert ctx.is_safe is True

    @pytest.mark.asyncio
    async def test_llm_returns_non_bool_is_safe_defaults_safe(self) -> None:
        """is_safe 字段非 bool(如字符串)时,应按安全处理。"""
        llm = _llm_returning(json.dumps({"is_safe": "false", "reason": "x"}))
        guard = ContentGuard(llm=llm)
        ctx = _ctx_with_input("任意输入")

        await guard.run(ctx)
        assert ctx.is_safe is True


class TestEmptyInput:
    """空输入边界。"""

    @pytest.mark.asyncio
    async def test_empty_input_is_safe_no_llm_call(self) -> None:
        """空 cleaned_input 应直接判安全,且不调用 LLM(省成本)。"""
        llm = _llm_returning(json.dumps({"is_safe": True}))
        guard = ContentGuard(llm=llm)
        ctx = DialogContext(user_input="", cleaned_input="")

        await guard.run(ctx)
        assert ctx.is_safe is True
        llm.chat.assert_not_awaited()
