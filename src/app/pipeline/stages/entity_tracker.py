"""阶段 4:实体抽取 ``EntityTracker``。

职责:从用户输入抽取关键实体(订单号/商品名/金额/手机号/人名),供:
- StrategyInjector 判断是否已有 order_id(决定是否启用工具);
- StreamGenerator 把实体填入工具调用参数;
- 后续画像更新(模块化记忆)。

为什么用 LLM 而非正则:
- 订单号格式多变,正则易漏;商品名是开放词表,正则无法覆盖;
- LLM 能结合上下文消歧("我要退那个手机"->商品名=当前讨论的手机);
- 一次调用抽取多类实体,比多套正则更省维护。

输出写 ``ctx.entities``(dict),key 为实体类型。已有实体不覆盖(增量合并):
多轮对话中用户可能分多次提供信息,保留之前的实体避免丢失。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage
from app.pipeline.stages._llm_json import parse_llm_json
from app.services.llm import LLMService

_logger = get_logger(__name__)

# 实体抽取 system prompt:枚举要抽的实体类型,要求 JSON,无则空值。
_ENTITY_SYSTEM_PROMPT = (
    "你是实体抽取器。从用户输入中抽取以下实体(找不到则对应值为空字符串):\n"
    "- order_id: 订单号(如 SO 开头编号)\n"
    "- product_name: 商品名\n"
    "- amount: 金额(数字,单位元)\n"
    "- phone: 手机号(11 位)\n"
    "- person_name: 人名(收件人/客服工号名等)\n"
    '只返回 JSON:{"entities": {"order_id": "...", "product_name": "...", ...}}。\n'
    "不要输出额外文字。"
)


class EntityTracker(BaseStage):
    """实体抽取阶段。

    Args:
        llm: 已初始化的 LLM 服务。
    """

    name = "EntityTracker"

    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def run(self, ctx: DialogContext) -> DialogContext:
        """抽取 ``ctx.cleaned_input`` 中的实体,合并写入 ``ctx.entities``。

        采用合并而非覆盖:多轮中用户补充信息时,保留已有实体,
        新实体覆盖同 key,实现"渐进式实体积累"。

        Args:
            ctx: 读 ``cleaned_input``,写 ``entities``。

        Returns:
            更新后的 ctx。
        """
        text = ctx.cleaned_input
        if not text:
            return ctx

        messages = [
            {"role": "system", "content": _ENTITY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        try:
            result = await self._llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            # 抽取失败不阻断:无实体也能继续对话(只是无法调工具)
            _logger.warning("entity_tracker.llm_unavailable.skip", error=str(exc))
            return ctx

        extracted = self._parse(result.get("content", ""))
        # 合并:新值非空才覆盖,避免空字符串冲掉已有实体
        for key, value in extracted.items():
            if value:
                ctx.entities[key] = value
        _logger.info("entity_tracker.done", entities=ctx.entities)
        return ctx

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        """解析实体 JSON,容错返回干净 dict。

        Args:
            content: LLM 输出文本。

        Returns:
            实体字典,值为字符串;解析失败返回空 dict。
        """
        parsed = parse_llm_json(content, default=None)
        if not isinstance(parsed, dict):
            return {}
        # LLM 可能返回 {"entities": {...}} 或直接 {...},两种都兼容
        entities = parsed.get("entities", parsed)
        if not isinstance(entities, dict):
            return {}
        # 统一转字符串并 strip,确保下游使用一致
        return {str(k): str(v).strip() for k, v in entities.items()}
