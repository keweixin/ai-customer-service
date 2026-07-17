"""策略注入阶段单元测试。

被测模块: ``app.pipeline.stages.strategy_injector``(StrategyInjector)

阶段 6:综合 intent + emotion + entities + retrieved_docs,决定本轮生成策略
(语气 / 是否启用工具 / 额外指令 / 系统提示补充),写入 ``ctx.strategy``。

实际实现是 ``BaseStage`` 子类,纯规则(不调 LLM),``async run(ctx)`` 读 ctx
多个中间产出,写 ``ctx.strategy`` 字典:
- tone: apologetic / professional / friendly / neutral
- use_tools: 是否启用 Function Calling
- extra_instruction: 额外生成指令
- system_prompt_addition: 拼到 system 消息末尾的补充(含 RAG 知识)

被测对象是纯规则逻辑,无外部依赖。
"""

from __future__ import annotations

import pytest

from app.pipeline.context import DialogContext
from app.pipeline.stages.strategy_injector import StrategyInjector


def _ctx(
    intent: str = "闲聊",
    entities: dict | None = None,
    retrieved_docs: list | None = None,
    emotion: str = "",
) -> DialogContext:
    """构造一个填好中间产出的 context。"""
    ctx = DialogContext()
    ctx.intent = intent
    ctx.entities = entities or {}
    ctx.retrieved_docs = retrieved_docs or []
    ctx.emotion = emotion
    return ctx


class TestQueryOrderStrategy:
    """查询订单意图 + order_id 实体触发工具。"""

    @pytest.mark.asyncio
    async def test_query_order_with_entity_triggers_tool(self) -> None:
        """查询订单 + entities 含 order_id 时,use_tools 应为 True。"""
        injector = StrategyInjector()
        ctx = _ctx(
            intent="查询订单",
            entities={"order_id": "SO20240101001"},
        )
        await injector.run(ctx)

        assert ctx.strategy["use_tools"] is True

    @pytest.mark.asyncio
    async def test_query_order_without_entity_no_tool(self) -> None:
        """查询订单但无 order_id 实体时,use_tools 应为 False(先追问)。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="查询订单", entities={})
        await injector.run(ctx)

        assert ctx.strategy["use_tools"] is False

    @pytest.mark.asyncio
    async def test_query_order_tone_is_professional(self) -> None:
        """查询订单意图 tone 应为 professional。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="查询订单", entities={})
        await injector.run(ctx)
        assert ctx.strategy["tone"] == "professional"


class TestComplaintStrategy:
    """投诉意图注入致歉语气。"""

    @pytest.mark.asyncio
    async def test_complaint_sets_apologetic_tone(self) -> None:
        """complaint 意图 tone 应为 apologetic(致歉)。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="投诉")
        await injector.run(ctx)

        assert ctx.strategy["tone"] == "apologetic"

    @pytest.mark.asyncio
    async def test_complaint_extra_instruction_mentions_handling(self) -> None:
        """complaint 的 extra_instruction 应提及投诉处理/转人工。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="投诉")
        await injector.run(ctx)

        instr = ctx.strategy["extra_instruction"]
        assert isinstance(instr, str)
        assert "投诉" in instr or "处理" in instr

    @pytest.mark.asyncio
    async def test_angry_emotion_overrides_tone_to_apologetic(self) -> None:
        """用户情绪 angry 时,无论意图 tone 都应被覆盖为 apologetic。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊", emotion="angry")
        await injector.run(ctx)
        assert ctx.strategy["tone"] == "apologetic"

    @pytest.mark.asyncio
    async def test_chat_tone_is_friendly(self) -> None:
        """闲聊意图 tone 应为 friendly。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊")
        await injector.run(ctx)
        assert ctx.strategy["tone"] == "friendly"


class TestRAGResultsStrategy:
    """有 RAG 检索结果时注入知识引用提示。"""

    @pytest.mark.asyncio
    async def test_with_rag_results_adds_knowledge_hint(self) -> None:
        """retrieved_docs 非空时,system_prompt_addition 应含知识库引用提示。"""
        injector = StrategyInjector()
        ctx = _ctx(
            intent="咨询商品",
            retrieved_docs=[
                {"content": "退换货 7 天内可办", "source": "policy.md"},
            ],
        )
        await injector.run(ctx)

        addition = ctx.strategy["system_prompt_addition"]
        assert isinstance(addition, str)
        assert "知识库" in addition or "参考" in addition
        assert "退换货 7 天内可办" in addition

    @pytest.mark.asyncio
    async def test_without_rag_results_no_knowledge_hint(self) -> None:
        """无 retrieved_docs 时,system_prompt_addition 不应含知识库引用。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊", retrieved_docs=[])
        await injector.run(ctx)

        addition = ctx.strategy["system_prompt_addition"]
        assert "知识库" not in addition
        assert "参考以下" not in addition

    @pytest.mark.asyncio
    async def test_rag_results_with_multiple_docs(self) -> None:
        """多个 RAG 片段都应拼进 system_prompt_addition。"""
        injector = StrategyInjector()
        docs = [
            {"content": "片段一", "source": "a"},
            {"content": "片段二", "source": "b"},
        ]
        ctx = _ctx(intent="咨询商品", retrieved_docs=docs)
        await injector.run(ctx)

        addition = ctx.strategy["system_prompt_addition"]
        assert "片段一" in addition
        assert "片段二" in addition


class TestStrategyShape:
    """strategy 字典结构完整性。"""

    @pytest.mark.asyncio
    async def test_strategy_has_all_keys(self) -> None:
        """strategy 应含 tone/use_tools/extra_instruction/system_prompt_addition 四键。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊")
        await injector.run(ctx)

        for key in ("tone", "use_tools", "extra_instruction", "system_prompt_addition"):
            assert key in ctx.strategy, f"strategy 缺 {key}"

    @pytest.mark.asyncio
    async def test_use_tools_is_bool(self) -> None:
        """use_tools 应为 bool 类型。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊")
        await injector.run(ctx)
        assert isinstance(ctx.strategy["use_tools"], bool)

    @pytest.mark.asyncio
    async def test_transfer_human_enables_tools(self) -> None:
        """转人工意图应启用工具(需调 transfer_human 创建工单)。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="转人工")
        await injector.run(ctx)
        assert ctx.strategy["use_tools"] is True

    @pytest.mark.asyncio
    async def test_run_returns_same_ctx(self) -> None:
        """run 应返回同一 ctx 实例(就地修改)。"""
        injector = StrategyInjector()
        ctx = _ctx(intent="闲聊")
        result = await injector.run(ctx)
        assert result is ctx
