"""阶段 6:策略注入 ``StrategyInjector``。

职责:综合 intent + emotion + entities + retrieved_docs,决定本轮生成策略
(语气 / 是否启用工具 / 额外指令 / 系统提示补充),写入 ``ctx.strategy``,
供 StreamGenerator 注入到 system 消息。

为什么需要策略层:
- 不同意图需要不同语气(投诉要道歉、咨询要专业),硬编码在生成 prompt 里难维护;
- 是否启用 Function Calling 取决于意图+实体(查订单且有 order_id 才启用工具),
  集中在此决策避免生成阶段臃肿;
- RAG 命中时把知识片段拼进系统提示,让生成阶段无需感知检索细节。

本阶段纯规则(不调 LLM):策略决策逻辑确定性强、延迟低、可测试,
LLM 留给真正需要自然语言能力的阶段(分类/抽取/生成)。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage

_logger = get_logger(__name__)

# 意图 -> 默认语气映射。投诉/退货退款倾向道歉与共情,咨询偏专业中性。
_INTENT_TONE: dict[str, str] = {
    "投诉": "apologetic",
    "退货退款": "apologetic",
    "咨询商品": "professional",
    "查询订单": "professional",
    "转人工": "neutral",
    "闲聊": "friendly",
}


class StrategyInjector(BaseStage):
    """策略注入阶段。

    无外部依赖(纯规则),不注入服务。读 ctx 的多个中间产物,产出
    ``ctx.strategy`` 字典。
    """

    name = "StrategyInjector"

    async def run(self, ctx: DialogContext) -> DialogContext:
        """根据上下文产出策略,写入 ``ctx.strategy``。

        策略字段:
        - tone: 语气(apologetic/professional/friendly/neutral)
        - use_tools: 是否向 LLM 声明工具(启用 Function Calling)
        - extra_instruction: 额外生成指令(如"优先安抚")
        - system_prompt_addition: 拼接到 system 消息末尾的补充(含 RAG 知识)

        Args:
            ctx: 读 intent/emotion/entities/retrieved_docs,写 strategy。

        Returns:
            更新后的 ctx。
        """
        intent = ctx.intent or "闲聊"
        emotion = ctx.emotion or "neutral"
        entities = ctx.entities or {}
        docs = ctx.retrieved_docs or []

        # 1. 语气:查表 + 情绪覆盖(愤怒用户无论意图都先安抚)
        tone = _INTENT_TONE.get(intent, "neutral")
        if emotion in ("angry", "frustrated"):
            tone = "apologetic"

        # 2. 是否启用工具:查询订单 且 已有 order_id 时才启用。
        #    只在"有明确可执行动作"时给工具,避免 LLM 乱调工具。
        use_tools = intent == "查询订单" and bool(entities.get("order_id"))
        # 转人工意图也启用工具(需调 transfer_human 创建工单)
        if intent == "转人工":
            use_tools = True

        # 3. 额外指令:按意图给生成阶段的引导
        extra_instruction = self._extra_instruction(intent, emotion)

        # 4. 系统提示补充:语气说明 + RAG 知识片段
        system_prompt_addition = self._build_system_addition(
            tone=tone, docs=docs, intent=intent
        )

        ctx.strategy = {
            "tone": tone,
            "use_tools": use_tools,
            "extra_instruction": extra_instruction,
            "system_prompt_addition": system_prompt_addition,
        }
        _logger.info(
            "strategy_injector.done",
            intent=intent,
            tone=tone,
            use_tools=use_tools,
            has_rag=bool(docs),
        )
        return ctx

    @staticmethod
    def _extra_instruction(intent: str, emotion: str) -> str:
        """按意图+情绪生成额外指令文本。

        Args:
            intent: 意图标签。
            emotion: 情绪标签。

        Returns:
            指令字符串(可为空)。
        """
        parts: list[str] = []
        if emotion in ("angry", "frustrated"):
            parts.append("用户情绪不佳,先表达理解与安抚,再解决问题。")
        if intent == "投诉":
            parts.append("认真对待投诉,给出明确处理方案或转人工,避免空话。")
        elif intent == "查询订单":
            parts.append("如需查订单,使用 query_order 工具获取真实数据,不要编造。")
        elif intent == "转人工":
            parts.append("如确认需转人工,使用 transfer_human 工具创建工单。")
        elif intent == "咨询商品":
            parts.append("结合知识库信息介绍商品,客观准确,不夸大。")
        return " ".join(parts)

    @staticmethod
    def _build_system_addition(
        *, tone: str, docs: list[dict[str, Any]], intent: str
    ) -> str:
        """拼装 system 消息末尾的补充:语气说明 + RAG 知识片段。

        Args:
            tone: 语气标签。
            docs: RAG 检索结果。
            intent: 意图(用于决定是否强调引用知识)。

        Returns:
            补充文本(可为空字符串)。
        """
        chunks: list[str] = []
        if tone:
            chunks.append(f"当前回复语气应保持:{tone}。")

        if docs:
            # 把检索片段拼成知识上下文,让生成阶段引用而非凭空生成
            kb_lines: list[str] = ["参考以下知识库信息(若相关则引用,无关则忽略):"]
            for i, doc in enumerate(docs, start=1):
                # 取 content 字段,兼容 source/score 用于追溯
                content = str(doc.get("content") or doc.get("text") or "").strip()
                if not content:
                    continue
                source = doc.get("source") or ""
                kb_lines.append(f"[{i}] {content}" + (f"(来源:{source})" if source else ""))
            if len(kb_lines) > 1:  # 有实际内容才加入
                chunks.append("\n".join(kb_lines))

        return "\n\n".join(chunks)
