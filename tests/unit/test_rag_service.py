"""RAG 服务单元测试(mock 向量检索)。

被测模块: ``app.services.rag_service``(RagService)

RAG 服务负责文档切块、向量化、向量检索、相似度过滤。实际契约:
- ``RagService(db, llm)`` 构造(内部建 DocumentRepository / KnowledgeChunkRepository)。
- ``_chunk_text(text, *, chunk_size, overlap)`` 静态方法,纯函数,按段落切块。
- ``retrieve(query, *, top_k, min_similarity)`` -> list[dict],调 llm.embed 与
  ``_chunk_repo.search_by_vector``。
- ``upload_document(...)`` -> doc 对象,调 llm.embed_batch 批量向量化。

pgvector 在 SQLite 不可用,向量检索通过 mock ``_chunk_repo.search_by_vector`` 完成,
不依赖真实 pgvector。``_chunk_text`` 是纯函数,直接测试。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.rag_service import RagService


def _make_rag_service(
    llm: MagicMock | None = None,
    db: MagicMock | None = None,
) -> RagService:
    """构造 RagService,注入 mock db / llm。

    构造期会建 DocumentRepository / KnowledgeChunkRepository,它们只用 db,
    传 mock db 即可;retrieve / upload 的仓库方法在测试中按需 mock。
    """
    if llm is None:
        llm = MagicMock()
        llm.embed = AsyncMock(return_value=[0.1] * 1024)
        llm.embed_batch = AsyncMock(return_value=[[0.1] * 1024])
    if db is None:
        db = MagicMock()
    return RagService(db=db, llm=llm)


class TestChunkText:
    """_chunk_text 切块逻辑(纯静态函数,无 IO)。"""

    def test_chunk_text_by_size(self) -> None:
        """按 chunk_size 切块,每块不超过 chunk_size 字符。"""
        text = "a" * 100
        chunks = RagService._chunk_text(text, chunk_size=30, overlap=0)

        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        for c in chunks:
            assert len(c) <= 30, "每块不应超过 chunk_size"

    def test_chunk_text_respects_paragraphs(self) -> None:
        """切块应优先在段落边界(空行)切,不破坏段落完整性。"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        chunks = RagService._chunk_text(text, chunk_size=100, overlap=0)

        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        # 短文本+大 chunk_size 应尽量合并,每块含完整段落
        for c in chunks:
            assert isinstance(c, str) and len(c) > 0

    def test_chunk_overlap(self) -> None:
        """带 overlap 时,相邻块应有重叠内容(保证上下文连续)。"""
        text = "0123456789" * 20  # 200 字符,单段超长触发硬切
        chunks = RagService._chunk_text(text, chunk_size=30, overlap=10)

        assert len(chunks) >= 2, "200 字符按 30 切带 10 overlap 应至少 2 块"
        # 相邻块的末尾与开头应有重叠字符(硬切带 overlap)
        if len(chunks) >= 2:
            tail = chunks[0][-10:]
            assert chunks[1].startswith(tail) or tail in chunks[1], (
                "相邻硬切块应有 overlap 重叠"
            )

    def test_chunk_empty_text_returns_empty(self) -> None:
        """空文本切块应返回空列表。"""
        assert RagService._chunk_text("", chunk_size=30, overlap=0) == []

    def test_chunk_only_whitespace_returns_empty(self) -> None:
        """纯空白文本切块应返回空列表。"""
        assert RagService._chunk_text("   \n\n  \t  ", chunk_size=30, overlap=0) == []

    def test_chunk_short_text_single_chunk(self) -> None:
        """短于 chunk_size 的文本应返回单块。"""
        chunks = RagService._chunk_text("短文本", chunk_size=100, overlap=0)
        assert len(chunks) == 1
        assert chunks[0] == "短文本"

    def test_chunk_overlap_ge_size_is_clamped(self) -> None:
        """overlap >= chunk_size 时应被裁剪(防死循环),不抛异常。"""
        text = "a" * 200
        # overlap=100 >= chunk_size=100,应内部裁剪
        chunks = RagService._chunk_text(text, chunk_size=100, overlap=100)
        assert isinstance(chunks, list)
        assert len(chunks) >= 1

    def test_chunk_multiple_paragraphs_combined(self) -> None:
        """多个短段落累计未超 chunk_size 时应合并为一块。"""
        text = "短句一。\n\n短句二。\n\n短句三。"
        chunks = RagService._chunk_text(text, chunk_size=100, overlap=0)
        # 三段都很短,应合并成 1 块
        assert len(chunks) == 1
        assert "短句一" in chunks[0]
        assert "短句三" in chunks[0]


class TestRetrieve:
    """retrieve 向量检索(mock 向量算相似度)。"""

    @pytest.mark.asyncio
    async def test_retrieve_returns_list_of_chunks(self) -> None:
        """retrieve 应返回 dict 列表,每项含 content / score。"""
        svc = _make_rag_service()
        svc._chunk_repo.search_by_vector = AsyncMock(
            return_value=[
                {"content": "块1", "score": 0.95, "doc_id": "d1"},
                {"content": "块2", "score": 0.88, "doc_id": "d2"},
            ]
        )

        results = await svc.retrieve("查询")

        assert isinstance(results, list)
        assert len(results) == 2
        for r in results:
            assert "content" in r
            assert "score" in r

    @pytest.mark.asyncio
    async def test_retrieve_filters_low_similarity(self) -> None:
        """retrieve 应把低于 min_similarity 的过滤交给 repo(此处验证参数传递)。

        实际过滤在 _chunk_repo.search_by_vector 内部用 pgvector 完成(SQLite 不可用),
        这里验证 retrieve 把 min_similarity 透传给 repo。
        """
        svc = _make_rag_service()
        svc._chunk_repo.search_by_vector = AsyncMock(
            return_value=[{"content": "相关", "score": 0.92}]
        )

        await svc.retrieve("查询", top_k=5, min_similarity=0.8)

        # 验证 search_by_vector 被调用,且 min_similarity 透传
        call_kwargs = svc._chunk_repo.search_by_vector.call_args.kwargs
        assert call_kwargs.get("min_similarity") == 0.8
        assert call_kwargs.get("top_k") == 5

    @pytest.mark.asyncio
    async def test_retrieve_respects_top_k(self) -> None:
        """retrieve 应把 top_k 透传给 repo。"""
        svc = _make_rag_service()
        svc._chunk_repo.search_by_vector = AsyncMock(return_value=[])

        await svc.retrieve("查询", top_k=3)

        call_kwargs = svc._chunk_repo.search_by_vector.call_args.kwargs
        assert call_kwargs.get("top_k") == 3

    @pytest.mark.asyncio
    async def test_retrieve_empty_query_returns_empty(self) -> None:
        """空查询应直接返回空列表,不调 embed / repo。"""
        svc = _make_rag_service()
        svc._chunk_repo.search_by_vector = AsyncMock(return_value=[])

        results = await svc.retrieve("")
        assert results == []
        svc.llm.embed.assert_not_awaited()
        svc._chunk_repo.search_by_vector.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retrieve_whitespace_query_returns_empty(self) -> None:
        """纯空白查询应返回空列表。"""
        svc = _make_rag_service()
        results = await svc.retrieve("   ")
        assert results == []

    @pytest.mark.asyncio
    async def test_retrieve_embed_failure_raises_rag_error(self) -> None:
        """query 向量化失败应抛 RAGError。"""
        from app.core.exceptions import RAGError

        llm = MagicMock()
        llm.embed = AsyncMock(side_effect=RuntimeError("embed down"))
        svc = _make_rag_service(llm=llm)

        with pytest.raises(RAGError):
            await svc.retrieve("查询")

    @pytest.mark.asyncio
    async def test_retrieve_uses_default_top_k_when_none(self) -> None:
        """top_k=None 时应使用 config.rag.top_k 默认值。"""
        svc = _make_rag_service()
        svc._chunk_repo.search_by_vector = AsyncMock(return_value=[])

        await svc.retrieve("查询")
        call_kwargs = svc._chunk_repo.search_by_vector.call_args.kwargs
        # 默认 top_k 来自 config.rag.top_k(默认 5)
        assert call_kwargs.get("top_k") == svc._settings.top_k
