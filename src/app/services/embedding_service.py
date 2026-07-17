"""Embedding 服务:对 LLMService.embed 的薄封装,提供批量与缓存。

为什么单独存在(尽管 LLMService 已有 embed/embed_batch):
- 关注点分离:RAG 切块向量化是高频且可缓存的子能力,独立服务便于在
  不侵入 LLMService 的前提下叠加缓存、批处理合并、降级策略;
- 批量合并:多个上传请求的切块可合并成更大的 batch 调上游,
  减少 round-trip(当前实现提供单次 batch,后续可扩展为队列合并);
- 缓存:相同文本的向量短期复用(LRU),避免重复计费;
  典型场景:重复上传相似文档 / 检索 query 命中历史 query。

接口契约:
- ``await emb.embed(text) -> list[float]`` -- 单条(命中缓存则直接返回)
- ``await emb.embed_batch(texts) -> list[list[float]]`` -- 批量
- ``await emb.embed_query(text) -> list[float]`` -- 检索 query 专用,
  不走缓存(query 通常一次性),强制实时向量化
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from app.core.exceptions import LLMError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.services.llm import LLMService

_logger = get_logger(__name__)


class _LRUCache:
    """极简 LRU 缓存(线程/协程不安全,但本服务实例单请求使用,无需锁)。

    用 OrderedDict 实现 LRU:move_to_end 标记最近访问,popitem(last=False)
    淘汰最久未用。仅缓存 embedding 向量,适合"重复文本不重复计费"场景。
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._store: OrderedDict[str, list[float]] = OrderedDict()

    def get(self, key: str) -> list[float] | None:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def set(self, key: str, value: list[float]) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self._maxsize:
            self._store.popitem(last=False)  # 淘汰最旧

    def __len__(self) -> int:
        return len(self._store)


class EmbeddingService:
    """Embedding 服务:封装 LLMService,叠加缓存与批量优化。

    Args:
        llm: 被包装的 LLMService(提供 embed/embed_batch)。
        cache_size: LRU 缓存容量;0 表示禁用缓存。
    """

    def __init__(self, llm: "LLMService", *, cache_size: int = 512) -> None:
        self._llm = llm
        self._cache = _LRUCache(maxsize=cache_size) if cache_size > 0 else None

    async def embed(self, text: str) -> list[float]:
        """单条文本向量化(命中缓存直接返回,不调上游)。

        空文本返回零向量(LLMService 内部已处理),不占缓存。
        """
        if not text.strip():
            return await self._llm.embed(text)
        if self._cache is not None:
            cached = self._cache.get(text)
            if cached is not None:
                _logger.debug("embedding 命中缓存", text_len=len(text))
                return cached
        vec = await self._llm.embed(text)
        if self._cache is not None:
            self._cache.set(text, vec)
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。

        命中缓存的文本跳过上游调用,未命中的合并成一个 batch 调上游,
        再按原顺序拼回结果。这样既省配额又保持顺序。
        """
        if not texts:
            return []
        results: list[list[float] | None] = [None] * len(texts)
        miss_indices: list[int] = []
        miss_texts: list[str] = []

        for i, t in enumerate(texts):
            if not t.strip():
                # 空串零向量占位(LLMService.embed_batch 也这么处理,这里提前填)
                results[i] = [0.0] * self._llm.embedding_dimension
                continue
            if self._cache is not None:
                cached = self._cache.get(t)
                if cached is not None:
                    results[i] = cached
                    continue
            miss_indices.append(i)
            miss_texts.append(t)

        if miss_texts:
            vecs = await self._llm.embed_batch(miss_texts)
            if len(vecs) != len(miss_texts):
                raise LLMError(
                    "批量 embedding 返回数量与输入不一致",
                    detail={"expected": len(miss_texts), "got": len(vecs)},
                )
            for idx, vec in zip(miss_indices, vecs):
                results[idx] = vec
                if self._cache is not None and miss_texts[miss_indices.index(idx)]:
                    self._cache.set(miss_texts[miss_indices.index(idx)], vec)

        # 此时 results 中不应有 None
        return [r if r is not None else [] for r in results]

    async def embed_query(self, text: str) -> list[float]:
        """检索 query 专用向量化:不走缓存,强制实时。

        query 通常是一次性、多变的,缓存命中率低且可能返回陈旧向量,
        因此单独提供不走缓存的入口。
        """
        return await self._llm.embed(text)

    def cache_stats(self) -> dict[str, int]:
        """返回缓存统计(调试/监控用)。"""
        return {"size": len(self._cache) if self._cache else 0}
