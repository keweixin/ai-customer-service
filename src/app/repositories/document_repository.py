"""知识库文档 Repository:封装 knowledge_docs 表的数据访问。

文档级元数据访问:标题 / 来源 / 切块计数。向量与切块内容在
knowledge_chunks 表(见 KnowledgeChunkRepository)。

对齐 API 层调用契约(knowledge.py / admin.py):
- ``DocumentRepository(db).list_all(limit=, offset=)`` -- 文档列表
- ``DocumentRepository(db).count()`` -- 管理后台统计

任务要求的文档 CRUD:create_doc / get_doc / list_docs / delete_doc /
update_chunks_count(上传切块后回填计数)。
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge_doc import KnowledgeDoc, SourceType
from app.repositories.base import BaseRepository

_logger = get_logger(__name__)


class DocumentRepository(BaseRepository[KnowledgeDoc]):
    """知识库文档表数据访问对象。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(KnowledgeDoc, db)

    async def create_doc(
        self,
        *,
        title: str,
        source_type: str | SourceType = SourceType.TEXT,
        content: str = "",
        metadata: Optional[dict[str, object]] = None,
    ) -> KnowledgeDoc:
        """创建文档记录(切块写入前的元信息占位)。

        source_type 接收 str 或枚举:API 层传字符串更自然,内部归一为枚举。
        content 存原始全文,便于回溯展示与重新切片。
        chunks_count 初始 0,由 update_chunks_count 在切块完成后回填。
        """
        if isinstance(source_type, str):
            source_type = SourceType(source_type)
        doc = await super().create(
            {
                "title": title,
                "source_type": source_type,
                "content": content,
                "metadata_": metadata,
                "chunks_count": 0,
            }  # type: ignore[arg-type]
        )
        _logger.info("文档已创建", doc_id=str(doc.id), title=title)
        return doc

    async def get_doc(self, doc_id: UUID) -> Optional[KnowledgeDoc]:
        """按 ID 取文档(等价于 get_by_id,语义化别名)。"""
        return await self.get_by_id(doc_id)

    # list_docs 等价于基类 list_all;保留别名贴合任务命名。
    list_docs = BaseRepository.list_all

    async def delete_doc(self, doc_id: UUID) -> bool:
        """删除文档(连带切块由 FK ondelete CASCADE 自动清理)。

        用 DELETE 而非先查后删,单条 SQL;返回是否命中。
        """
        stmt = delete(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        deleted = (result.rowcount or 0) > 0
        if deleted:
            _logger.info("文档已删除", doc_id=str(doc_id))
        return deleted

    async def update_chunks_count(self, doc_id: UUID, count: int) -> bool:
        """回填文档的切块数量(上传流程末尾调用)。

        也可传增量:count>0 表示设置为 count。当前实现为"设置"语义,
        调用方在切块全部入库后传入实际块数。
        """
        stmt = (
            update(KnowledgeDoc)
            .where(KnowledgeDoc.id == doc_id)
            .values(chunks_count=count)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return (result.rowcount or 0) > 0
