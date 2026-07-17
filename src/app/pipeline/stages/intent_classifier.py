"""阶段 3:意图分类 ``IntentClassifier``。

职责:识别用户本轮意图(咨询商品/查询订单/退货退款/投诉/闲聊/转人工),
供 StrategyInjector 选策略、RagRetriever 调检索范围。

为什么用 LLM 而非规则:
- 客服意图边界模糊(如"这个能退吗"既可能是退货咨询也可能是售后投诉),
  规则难以覆盖自然语言变体;
- LLM 能给出置信度,低置信时兜底为"闲聊",避免错误路由放大问题;
- 输出结构化 JSON,便于下游消费。

置信度低于 0.5 兜底为"闲聊":低置信意味着模型不确定,闲聊是最安全的
降级(不会误触发订单查询/转人工等有副作用的动作)。
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage
from app.pipeline.stages._llm_json import parse_llm_json
from app.services.llm import LLMService

_logger = get_logger(__name__)

# 兜底意图:置信度不足或解析失败时使用。闲聊不触发工具/转人工,风险最低。
_FALLBACK_INTENT = "闲聊"

# 置信度阈值:低于此值降级为闲聊,避免错误路由。
_CONFIDENCE_THRESHOLD = 0.5

# 意图分类 system prompt:枚举标签、要求 JSON、要求置信度。
_INTENT_SYSTEM_PROMPT = (
    "你是客服意图分类器。将用户输入分类为以下之一:\n"
    "- 咨询商品:了解商品信息、规格、价格、推荐\n"
    "- 查询订单:查订单状态、物流、进度\n"
    "- 退货退款:退换货、退款、售后\n"
    "- 投诉:不满、纠纷、要求处理\n"
    "- 闲聊:寒暄、无关、模糊\n"
    "- 转人工:明确要求人工、超出 AI 能力\n"
    '只返回 JSON:{"intent": "标签", "confidence": 0.0-1.0}。\n'
    "不要输出额外文字。"
)


class IntentClassifier(BaseStage):
    """意图分类阶段。

    Args:
        llm: 已初始化的 LLM 服务。
    """

    name = "IntentClassifier"

    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def run(self, ctx: DialogContext) -> DialogContext:
        """分类 ``ctx.cleaned_input`` 的意图,写入 ``ctx.intent``。

        Args:
            ctx: 读 ``cleaned_input``,写 ``intent``(及置信度到 metadata)。

        Returns:
            更新后的 ctx。
        """
        text = ctx.cleaned_input
        if not text:
            ctx.intent = _FALLBACK_INTENT
            return ctx

        messages = [
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        try:
            result = await self._llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            # LLM 不可用:降级闲聊,不阻断对话主流程
            _logger.warning("intent_classifier.llm_unavailable.fallback", error=str(exc))
            ctx.intent = _FALLBACK_INTENT
            return ctx

        intent, confidence = self._parse(result.get("content", ""))
        ctx.intent = intent
        # 置信度写入专用字段,供策略/监控消费;同时镜像到 metadata 便于扩展
        ctx.intent_confidence = confidence
        ctx.metadata["intent_confidence"] = confidence
        _logger.info("intent_classifier.done", intent=intent, confidence=confidence)
        return ctx

    @staticmethod
    def _parse(content: str) -> tuple[str, float]:
        """解析意图 JSON,低置信度/解析失败兜底为闲聊。

        Args:
            content: LLM 输出文本。

        Returns:
            (intent, confidence) 元组。
        """
        parsed = parse_llm_json(content, default=None)
        if not isinstance(parsed, dict):
            return _FALLBACK_INTENT, 0.0
        intent = str(parsed.get("intent") or "").strip()
        # 置信度做范围保护:非数/越界都按 0 处理
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if not intent or confidence < _CONFIDENCE_THRESHOLD:
            # 低置信或解析不出意图:兜底闲聊,避免错误路由
            return _FALLBACK_INTENT, confidence
        return intent, confidence
