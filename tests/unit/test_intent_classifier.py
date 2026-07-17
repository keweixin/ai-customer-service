"""意图识别阶段单元测试(mock LLM)。

被测模块: ``app.pipeline.stages.intent_classifier``(IntentClassifier)

阶段 3:用 LLM 把用户输入分类到固定意图集(中文标签:咨询商品/查询订单/退货退款/
投诉/闲聊/转人工)。实际实现是 ``BaseStage`` 子类,``async run(ctx)`` 调
``llm.chat(messages)``(返回 ``{"content": "..."}``),解析 JSON
``{"intent": str, "confidence": float}``,写回 ``ctx.intent`` 与
``ctx.intent_confidence``。

兜底策略:置信度 < 0.5 或解析失败/LLM 异常时,回退为"闲聊"(风险最低)。
LLM 全程 mock。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pipeline.context import DialogContext
from app.pipeline.stages.intent_classifier import (
    IntentClassifier,
    _CONFIDENCE_THRESHOLD,
    _FALLBACK_INTENT,
)


def _llm_returning(content: str) -> MagicMock:
    """构造 mock LLM,chat() 返回 {"content": content}。"""
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"content": content, "tool_calls": [], "usage": {}})
    return llm


def _ctx_with_input(text: str) -> DialogContext:
    """构造 cleaned_input 就绪的 context。"""
    return DialogContext(user_input=text, cleaned_input=text)


class TestQueryOrderIntent:
    """查询订单意图识别。"""

    @pytest.mark.asyncio
    async def test_classify_query_order(self) -> None:
        """LLM 返回高置信查询订单意图时,ctx.intent 应为"查询订单"。"""
        llm = _llm_returning(
            json.dumps({"intent": "查询订单", "confidence": 0.92})
        )
        clf = IntentClassifier(llm=llm)

        ctx = _ctx_with_input("我的订单 SO20240101001 到哪了?")
        await clf.run(ctx)

        assert ctx.intent == "查询订单"
        assert ctx.intent_confidence == 0.92

    @pytest.mark.asyncio
    async def test_query_order_confidence_stored(self) -> None:
        """置信度应写入 ctx.intent_confidence 供策略/监控消费。"""
        llm = _llm_returning(
            json.dumps({"intent": "查询订单", "confidence": 0.88})
        )
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("查订单")
        await clf.run(ctx)

        assert isinstance(ctx.intent_confidence, float)
        assert ctx.intent_confidence == 0.88


class TestComplaintIntent:
    """投诉意图识别。"""

    @pytest.mark.asyncio
    async def test_classify_complaint(self) -> None:
        """LLM 返回高置信投诉意图时,ctx.intent 应为"投诉"。"""
        llm = _llm_returning(json.dumps({"intent": "投诉", "confidence": 0.85}))
        clf = IntentClassifier(llm=llm)

        ctx = _ctx_with_input("你们的服务太差了,我要投诉!")
        await clf.run(ctx)

        assert ctx.intent == "投诉"
        assert ctx.intent_confidence >= _CONFIDENCE_THRESHOLD


class TestLowConfidenceFallback:
    """低置信度回退到"闲聊"。"""

    @pytest.mark.asyncio
    async def test_low_confidence_defaults_to_chat(self) -> None:
        """confidence < 阈值时,intent 应回退为"闲聊"。"""
        llm = _llm_returning(
            json.dumps({"intent": "查询订单", "confidence": 0.2})
        )
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("嗯...那个...")
        await clf.run(ctx)

        assert ctx.intent == _FALLBACK_INTENT, "低置信度应回退闲聊"

    @pytest.mark.asyncio
    async def test_confidence_at_threshold_keeps_intent(self) -> None:
        """置信度恰好 >= 阈值时应保留原意图(边界)。"""
        llm = _llm_returning(
            json.dumps({"intent": "投诉", "confidence": _CONFIDENCE_THRESHOLD})
        )
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("不太满意")
        await clf.run(ctx)
        assert ctx.intent == "投诉"


class TestInvalidLLMResponse:
    """LLM 返回非法 JSON / 异常时的容错。"""

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_json(self) -> None:
        """LLM 返回非 JSON 时,intent 应回退为"闲聊"。"""
        llm = _llm_returning("这不是 JSON")
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("任意输入")
        await clf.run(ctx)

        assert ctx.intent == _FALLBACK_INTENT
        assert ctx.intent_confidence == 0.0

    @pytest.mark.asyncio
    async def test_llm_returns_empty_content(self) -> None:
        """LLM 返回空 content 时回退闲聊。"""
        llm = _llm_returning("")
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("任意输入")
        await clf.run(ctx)
        assert ctx.intent == _FALLBACK_INTENT

    @pytest.mark.asyncio
    async def test_llm_raises_falls_back_to_chat(self) -> None:
        """LLM 抛异常时回退闲聊,不向上抛。"""
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("任意输入")
        await clf.run(ctx)
        assert ctx.intent == _FALLBACK_INTENT

    @pytest.mark.asyncio
    async def test_llm_returns_empty_intent_falls_back(self) -> None:
        """LLM 返回空 intent 字符串时回退闲聊。"""
        llm = _llm_returning(json.dumps({"intent": "", "confidence": 0.99}))
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("任意输入")
        await clf.run(ctx)
        assert ctx.intent == _FALLBACK_INTENT

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_confidence_falls_back(self) -> None:
        """LLM 返回非数值 confidence 时回退闲聊(置信度按 0 处理)。"""
        llm = _llm_returning(
            json.dumps({"intent": "查询订单", "confidence": "很高"})
        )
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("任意输入")
        await clf.run(ctx)
        assert ctx.intent == _FALLBACK_INTENT

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_range(self) -> None:
        """置信度越界(>1 或 <0)应被裁剪到 [0, 1]。"""
        llm = _llm_returning(
            json.dumps({"intent": "查询订单", "confidence": 1.5})
        )
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("查订单")
        await clf.run(ctx)
        assert 0.0 <= ctx.intent_confidence <= 1.0


class TestResultFields:
    """run 写入的字段完整性。"""

    @pytest.mark.asyncio
    async def test_intent_and_confidence_both_written(self) -> None:
        """run 应同时写 intent 与 intent_confidence。"""
        llm = _llm_returning(json.dumps({"intent": "闲聊", "confidence": 0.9}))
        clf = IntentClassifier(llm=llm)
        ctx = _ctx_with_input("你好")
        await clf.run(ctx)

        assert isinstance(ctx.intent, str) and len(ctx.intent) > 0
        assert isinstance(ctx.intent_confidence, float)

    @pytest.mark.asyncio
    async def test_empty_input_falls_back(self) -> None:
        """空 cleaned_input 应直接回退闲聊,不调 LLM。"""
        llm = _llm_returning(json.dumps({"intent": "投诉", "confidence": 0.9}))
        clf = IntentClassifier(llm=llm)
        ctx = DialogContext(user_input="", cleaned_input="")
        await clf.run(ctx)

        assert ctx.intent == _FALLBACK_INTENT
        llm.chat.assert_not_awaited()
