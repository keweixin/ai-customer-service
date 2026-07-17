"""知识库切块 Repository:封装 knowledge_chunks 表的数据访问。

RAG 检索的核心持久化层:存文本切片 + pgvector 向量。

关键能力:
- ``create_chunk(doc_id=, chunk_index=, content=, embedding=, metadata=)``
  批量/单条写入切块(向量随文本一起入库)。
- ``get_chunks_by_doc(doc_id)`` -- 按文档取全部切块(回溯/调试)。
- ``search_by_vector(embedding, top_k=, min_similarity=)`` -- pgvector 余弦
  检索:用 ``<=>`` 操作符算余弦距离 ORDER BY,过滤相似度阈值。
- ``search_by_keyword(keyword, limit=)`` -- 全文兜底检索(ILIKE),
  在向量检索不可用或为 0 命中时提供关键词召回。

pgvector 检索说明:
- ``<=>`` 是余弦距离(0=完全相同,2=完全相反),距离越小越相似;
- 相似度 = 1 - 距离,故过滤条件为 ``1 - (embedding <=> q) >= min_similarity``;
- HNSW 索引(ix_knowledge_chunks_embedding_hnsw, vector_cosine_ops)
  加速近似最近邻,与 <=> 操作符匹配,查询自动命中索引。
- 过滤未向量化的块(embedding IS NULL),保证"先入库后回填"流水线安全。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge_chunk import KnowledgeChunk
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class KnowledgeChunkRepository(BaseRepository[KnowledgeChunk]):
    """知识库切块表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(KnowledgeChunk, db)

    async def create_chunk(
        self,
        *,
        doc_id: UUID,
        chunk_index: int,
        content: str,
        embedding: list[float],
        metadata: Optional[dict[str, Any]] = None,
    ) -> KnowledgeChunk:
        """写入一条切块(文本 + 向量同时入库)。"""
        return await super().create(
            {
                "doc_id": doc_id,
                "chunk_index": chunk_index,
                "content": content,
                "embedding": embedding,
                "metadata_": metadata,
            }  # type: ignore[arg-type]
        )

    async def bulk_create(self, chunks: list[dict[str, Any]]) -> list[KnowledgeChunk]:
        """批量写入切块(上传文档时多块一起入库,减少 round-trip)。

        每个 chunk dict 应含 doc_id / chunk_index / content / embedding / metadata_。
        逐条 add 后一次 flush,返回插入的对象列表。
        """
        objs = [KnowledgeChunk(**c) for c in chunks]
        self.db.add_all(objs)
        await self.db.flush()
        for o in objs:
            await self.db.refresh(o)
        _logger.info("批量写入切块", count=len(objs))
        return objs

    async def get_chunks_by_doc(self, doc_id: UUID) -> list[KnowledgeChunk]:
        """按文档取全部切块,按 chunk_index 正序(回溯/调试/重新拼接)。"""
        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.doc_id == doc_id)
            .order_by(KnowledgeChunk.chunk_index.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_by_doc(self, doc_id: UUID) -> int:
        """统计某文档的切块数(回填 chunks_count 用)。"""
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.doc_id == doc_id)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def search_by_vector(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float = 0.7,
    ) -> list[dict[str, Any]]:
        """pgvector 余弦语义检索,返回 top-k 命中切片。

        用原生 SQL 触发 ``<=>`` 操作符与 HNSW 索引;SQLAlchemy 表达式层对
        pgvector 距离算子的封装在版本间有差异,直接用 text 更稳定可控。

        Args:
            embedding: 查询向量(维度须与列一致,否则 PG 报错)。
            top_k: 返回条数上限。
            min_similarity: 最低相似度阈值(0~1),低于此值的过滤掉;
                相似度 = 1 - 余弦距离。

        Returns:
            list[dict],每项含 content / score(相似度,1.0 最佳)/ doc_id /
            doc_title(关联 knowledge_docs 取标题)/ chunk_id / chunk_index。
        """
        # 原生 SQL:用 <=> 算距离,1 - 距离 即相似度;JOIN 文档表取标题。
        # 参数用命名绑定,asyncpg 支持 list 直接传给 vector 类型。
        sql = text(
            """
            SELECT
                c.id            AS chunk_id,
                c.content       AS content,
                c.chunk_index   AS chunk_index,
                c.doc_id        AS doc_id,
                d.title         AS doc_title,
                1 - (c.embedding <=> :q_vec) AS score
            FROM knowledge_chunks c
            JOIN knowledge_docs d ON d.id = c.doc_id
            WHERE c.embedding IS NOT NULL
              AND 1 - (c.embedding <=> :q_vec) >= :min_sim
            ORDER BY c.embedding <=> :q_vec
            LIMIT :top_k
            """
        )
        result = await self.db.execute(
            sql,
            {
                "q_vec": embedding,
                "min_sim": min_similarity,
                "top_k": top_k,
            },
        )
        rows = result.mappings().all()
        # mappings 行是 RowMapping,转普通 dict 便于上层直接序列化
        hits = [dict(row) for row in rows]
        _logger.info(
            "向量检索完成",
            top_k=top_k,
            min_similarity=min_similarity,
            hit_count=len(hits),
        )
        return hits

    async def search_by_keyword(
        self, keyword: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """关键词全文兜底检索(ILIKE 模糊匹配)。

        场景:向量检索未命中或 embedding 服务不可用时,用关键词召回。
        ILIKE 不区分大小写;用 %keyword% 两端通配,召回含关键词的切片。
        性能注意:全表 ILIKE 扫描,大库应叠加 PG 全文索引(tsvector + GIN),
        此处提供基础实现,后续可替换为 ts_rank 排序的全文检索。

        Returns:
            list[dict],每项含 content / doc_id / doc_title / chunk_id /
            chunk_index(score 字段留空,关键词检索不产生相似度分数)。
        """
        like_pattern = f"%{keyword}%"
        sql = text(
            """
            SELECT
                c.id            AS chunk_id,
                c.content       AS content,
                c.chunk_index   AS chunk_index,
                c.doc_id        AS doc_id,
                d.title         AS doc_title
            FROM knowledge_chunks c
            JOIN knowledge_docs d ON d.id = c.doc_id
            WHERE c.content ILIKE :kw
            ORDER BY c.chunk_index
            LIMIT :lim
            """
        )
        result = await self.db.execute(
            sql, {"kw": like_pattern, "lim": limit}
        )
        rows = result.mappings().all()
        hits = [dict(row) for row in rows]
        _logger.info(
            "关键词检索完成",
            keyword=keyword,
            hit_count=len(hits),
        )
        return hits

    async def delete_by_doc(self, doc_id: UUID) -> int:
        """删除某文档的全部切块(通常由 FK CASCADE 自动完成,显式版备用)。

        Returns:
            删除行数。
        """
        from sqlalchemy import delete as sa_delete

        stmt = sa_delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        return int(result.rowcount or 0)
