"""阶段 5:知识库检索 ``RagRetriever``。

职责:用清洗后的用户输入做向量检索,召回相关知识片段,供生成阶段做检索增强
(RAG),让回复基于商品文档/FAQ/SOP,而非纯靠模型内部知识(易过时/幻觉)。

依赖注入 ``rag_service`` 而非直接持有 LLM/DB:检索逻辑(向量化 + 相似度 +
过滤)封装在 RagService 里,本阶段只负责"调一次 retrieve 并写回 ctx",
职责单一,便于替换检索实现。

检索失败不阻断:RAG 是增强项,无知识片段也能生成(退化为模型自身知识),
故异常时清空 retrieved_docs 继续流程,仅记日志。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage

if TYPE_CHECKING:
    # 仅类型注解用,运行期不 import 避免硬依赖( RagService 由 deps 注入,
    # 此处用 Protocol 风格的鸭子类型即可,真实类在 app.services.rag_service)
    from app.services.rag_service import RagService  # noqa: F401

_logger = get_logger(__name__)


class RagRetriever(BaseStage):
    """RAG 检索阶段。

    Args:
        rag_service: 提供 ``async retrieve(query) -> list[dict]`` 接口的服务。
            用鸭子类型而非具体类,便于测试用 fake 注入。
    """

    name = "RagRetriever"

    def __init__(self, rag_service: "RagService") -> None:
        self._rag_service = rag_service

    async def run(self, ctx: DialogContext) -> DialogContext:
        """检索知识片段,写入 ``ctx.retrieved_docs``。

        Args:
            ctx: 读 ``cleaned_input``,写 ``retrieved_docs``。

        Returns:
            更新后的 ctx。
        """
        query = ctx.cleaned_input
        if not query:
            # 无输入不检索,避免无效向量计算
            ctx.retrieved_docs = []
            return ctx

        try:
            docs = await self._rag_service.retrieve(query)
        except Exception as exc:  # noqa: BLE001
            # 检索失败:清空结果继续流程,RAG 降级为无增强,不阻断对话
            _logger.warning("rag_retriever.failed.fallback_empty", error=str(exc))
            ctx.retrieved_docs = []
            return ctx

        # 归一化:保证是 list[dict],防御 retrieve 实现返回 None/非列表
        if not isinstance(docs, list):
            ctx.retrieved_docs = []
        else:
            ctx.retrieved_docs = [d for d in docs if isinstance(d, dict)]

        _logger.info(
            "rag_retriever.done",
            query=query[:80],
            hit_count=len(ctx.retrieved_docs),
        )
        return ctx
