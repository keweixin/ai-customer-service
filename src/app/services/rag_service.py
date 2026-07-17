"""RAG 服务:知识库管理 + 语义检索。

业务核心能力之一:把文档切块向量化存入 pgvector,检索时按 query 向量做
余弦最近邻召回,过滤相似度阈值,返回 top-k 片段供 Pipeline 注入 prompt。

职责边界:
- 文档上传:切块 -> 向量化 -> 落库(含 chunks_count 回填);
- 文档管理:列表 / 删除(删除连带切块由 FK CASCADE);
- 语义检索:query 向量化 -> pgvector <=> 检索 -> 阈值过滤 -> top-k;
- 关键词兜底:向量检索不可用或召回为 0 时,ILIKE 召回。

对齐 API 层契约(knowledge.py / deps.py):
- ``RagService(db=db, llm=llm)`` -- 构造期注入 db 与 LLMService
- ``await rag.upload_document(payload)`` -- payload 为 DocumentUpload schema
- ``await rag.delete_document(doc_id) -> bool``
- ``await rag.retrieve(query=...) -> list[chunk]`` -- chunk 可被 ChunkResponse 序列化

切块策略(_chunk_text):
- 优先按段落切(\\n\\n),不破坏段落完整性:段落小于 chunk_size 直接成块;
  段落大于 chunk_size 时再按 chunk_size 硬切并带 overlap;
- 相邻小块按 overlap 重叠,保证跨块语义连贯(如同一句被切到两块时,
  两块都含部分重叠内容,检索召回率更高)。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import NotFoundError, RAGError
from app.core.logging import get_logger
from app.repositories.document_repository import DocumentRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository

_logger = get_logger(__name__)


class RagService:
    """RAG 知识库服务:文档管理 + 向量检索。

    Args:
        db: 请求级数据库会话(由 deps.get_rag_service 注入)。
        llm: LLMService 实例,提供 embed/embed_batch 向量化能力。
    """

    def __init__(self, db: AsyncSession, llm: Any) -> None:
        self.db = db
        self.llm = llm
        self._settings = get_settings().rag
        self._doc_repo = DocumentRepository(db)
        self._chunk_repo = KnowledgeChunkRepository(db)
        # Embedding 复用 LLMService;如需缓存可在此包装 EmbeddingService,
        # 但 upload_document 已走 batch,缓存收益有限,暂直接用 llm。
        self._embedding_service = None  # 懒构造,需要时再建

    # ------------------------------------------------------------------
    # 文档上传
    # ------------------------------------------------------------------
    async def upload_document(
        self,
        payload: Any,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        source_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Any:
        """上传文档:切块 -> 向量化 -> 落库。

        支持两种调用方式(对齐 API 层与任务规范):
        1. 传 schema 对象 payload(DocumentUpload,含 title/content/source_type);
           此时 title/content 等关键字参数应留空。
        2. 直接传关键字参数(title=, content=, source_type=, metadata=)。

        Args:
            payload: DocumentUpload schema 或 None。
            title/content/source_type/metadata: 关键字形式直传。

        Returns:
            创建的 KnowledgeDoc 文档对象(含 id / chunks_count 等)。

        Raises:
            RAGError: 内容为空或向量化失败。
        """
        # 归一化参数:schema 优先,关键字补充
        if payload is not None:
            # 兼容 Pydantic model 与普通对象:用 getattr 取属性
            doc_title = title or getattr(payload, "title", None)
            doc_content = content or getattr(payload, "content", None)
            doc_source = source_type or getattr(payload, "source_type", None) or "text"
            doc_meta = metadata or getattr(payload, "metadata", None)
        else:
            doc_title = title
            doc_content = content
            doc_source = source_type or "text"
            doc_meta = metadata

        if not doc_title or not doc_content:
            raise RAGError("文档标题与内容不能为空")

        # 1. 创建文档记录(chunks_count=0 占位)
        doc = await self._doc_repo.create_doc(
            title=doc_title,
            source_type=doc_source,
            content=doc_content,
            metadata=doc_meta,
        )

        # 2. 切块
        chunks = self._chunk_text(
            doc_content,
            chunk_size=self._settings.chunk_size,
            overlap=self._settings.chunk_overlap,
        )
        if not chunks:
            # 内容无法切出有效块(如纯空白),更新计数为 0 并返回
            _logger.warning("文档切出 0 块", doc_id=str(doc.id), title=doc_title)
            return doc

        # 3. 批量向量化(一次 batch 调用,减少 round-trip)
        try:
            embeddings = await self.llm.embed_batch(chunks)
        except Exception as exc:
            # 向量化失败:回滚已创建的文档(由上层事务控制),抛 RAGError
            _logger.error("文档向量化失败", doc_id=str(doc.id), error=str(exc))
            raise RAGError("文档向量化失败", detail={"doc_id": str(doc.id)}) from exc

        if len(embeddings) != len(chunks):
            raise RAGError(
                "向量化数量与切块数量不一致",
                detail={"chunks": len(chunks), "embeddings": len(embeddings)},
            )

        # 4. 批量写入切块(文本 + 向量)
        chunk_dicts = [
            {
                "doc_id": doc.id,
                "chunk_index": idx,
                "content": text,
                "embedding": emb,
                "metadata_": {"char_offset": idx * self._settings.chunk_size},
            }
            for idx, (text, emb) in enumerate(zip(chunks, embeddings))
        ]
        await self._chunk_repo.bulk_create(chunk_dicts)

        # 5. 回填 chunks_count
        await self._doc_repo.update_chunks_count(doc.id, len(chunks))

        _logger.info(
            "文档上传完成",
            doc_id=str(doc.id),
            title=doc_title,
            chunks=len(chunks),
        )
        return doc

    # ------------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------------
    async def list_documents(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[Any]:
        """列出知识库文档(元信息,不含向量)。"""
        return await self._doc_repo.list_all(limit=limit, offset=offset)

    async def delete_document(self, doc_id: UUID) -> bool:
        """删除文档及其全部切块(切块由 FK CASCADE 自动清理)。

        Returns:
            是否删除成功(文档不存在返回 False)。
        """
        deleted = await self._doc_repo.delete_doc(doc_id)
        if deleted:
            _logger.info("文档已删除(含切块)", doc_id=str(doc_id))
        return deleted

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------
    async def retrieve(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """向量语义检索:返回 top-k 相关切片。

        Args:
            query: 用户查询文本。
            top_k: 返回条数;None 用 config.rag.top_k。
            min_similarity: 最低相似度阈值;None 用 config.rag.min_similarity。

        Returns:
            list[dict],每项含 content / score / doc_id / doc_title /
            chunk_id / chunk_index,可直接被 ChunkResponse.model_validate
            或 Pipeline 的 retrieved_docs 消费。
        """
        if not query or not query.strip():
            return []

        k = top_k or self._settings.top_k
        sim = min_similarity if min_similarity is not None else self._settings.min_similarity

        # query 向量化(实时,不走缓存)
        try:
            query_vec = await self.llm.embed(query)
        except Exception as exc:
            _logger.error("检索 query 向量化失败", query=query, error=str(exc))
            raise RAGError("检索向量化失败") from exc

        hits = await self._chunk_repo.search_by_vector(
            query_vec, top_k=k, min_similarity=sim
        )
        _logger.info(
            "RAG 检索完成",
            query=query,
            top_k=k,
            min_similarity=sim,
            hit_count=len(hits),
        )
        return hits

    async def search_by_keyword(
        self, keyword: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """关键词全文兜底检索(ILIKE)。

        场景:embedding 服务不可用、或向量检索召回为 0 时,用关键词召回。
        返回结构同 retrieve,但无 score 字段(关键词检索不产生相似度)。
        """
        if not keyword or not keyword.strip():
            return []
        return await self._chunk_repo.search_by_keyword(keyword, limit=limit)

    # ------------------------------------------------------------------
    # 切块逻辑(纯函数,可单测)
    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_text(
        text: str, *, chunk_size: int = 500, overlap: int = 50
    ) -> list[str]:
        """文本切块:优先按段落切,不破坏段落完整性。

        策略:
        1. 按 \\n\\n(空行)切段落;
        2. 累积段落,一旦累计长度 >= chunk_size 就成块;
        3. 超长单段(> chunk_size)按 chunk_size 硬切并带 overlap;
        4. 相邻块通过 overlap 重叠:新块开头复制上一块末尾 overlap 字符,
           保证跨块语义连贯。

        Args:
            text: 原始文本。
            chunk_size: 目标块大小(字符数)。
            overlap: 块间重叠字符数(必须 < chunk_size)。

        Returns:
            切块列表;空文本或纯空白返回 []。
        """
        if not text or not text.strip():
            return []
        # 防御:overlap 必须小于 chunk_size,否则死循环
        if overlap >= chunk_size:
            overlap = max(0, chunk_size // 4)

        # 1. 按空行切段落(保留段内换行);连续空行合并
        raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        paragraphs = [p for p in raw_paragraphs if p]

        chunks: list[str] = []
        buffer: str = ""

        def _flush(buf: str) -> None:
            """把累积块加入结果(非空才加)。"""
            if buf.strip():
                chunks.append(buf.strip())

        def _hard_split(long_text: str) -> list[str]:
            """对超长段按 chunk_size 硬切并带 overlap。"""
            parts: list[str] = []
            start = 0
            n = len(long_text)
            step = chunk_size - overlap
            while start < n:
                end = start + chunk_size
                piece = long_text[start:end]
                if piece.strip():
                    parts.append(piece.strip())
                if end >= n:
                    break
                start += step
            return parts

        for para in paragraphs:
            # 超长单段:硬切后逐块吸收进 buffer 流程
            if len(para) > chunk_size:
                # 先把当前 buffer 收尾成块
                if buffer:
                    _flush(buffer)
                    buffer = ""
                for piece in _hard_split(para):
                    # 硬切出的 piece 都 <= chunk_size,按正常流程处理
                    if len(buffer) + len(piece) + 1 <= chunk_size or not buffer:
                        buffer = f"{buffer}\n{piece}".strip() if buffer else piece
                    else:
                        _flush(buffer)
                        # overlap:新块带上块末尾
                        buffer = (buffer[-overlap:] + " " + piece) if overlap else piece
                continue

            # 正常段落:尝试并入 buffer
            if not buffer:
                buffer = para
            elif len(buffer) + len(para) + 1 <= chunk_size:
                # +1 为换行符
                buffer = f"{buffer}\n{para}"
            else:
                # 当前 buffer 已够一块,先收尾再开新块(带 overlap)
                _flush(buffer)
                if overlap and len(buffer) > overlap:
                    buffer = buffer[-overlap:] + "\n" + para
                else:
                    buffer = para

        # 收尾最后一块
        _flush(buffer)
        return chunks
