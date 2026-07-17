"""知识库文档块模型(向量存储)。

每个 chunk 是一段文本 + 其 embedding 向量,是 RAG 检索的最小单元。
向量列用 pgvector 的 Vector(1024) 类型,与 .env.example 中
EMBEDDING_DIMENSION=1024 对齐(火山方舟 embedding 输出 1024 维)。
检索时用 <=>(余弦距离)算子,HNSW 索引加速近似最近邻查询。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

# pgvector 提供 SQLAlchemy 类型适配器,使 ORM 可直接声明向量列。
from pgvector.sqlalchemy import Vector

from .base import Base

if TYPE_CHECKING:
    from .knowledge_doc import KnowledgeDoc

# 向量维度:与方舟 embedding 模型输出维度一致(见 .env.example)。
# 单点定义避免散落各处不同步;若模型升级需全局改维度,在此改一处。
EMBEDDING_DIMENSION = 1024


class KnowledgeChunk(Base):
    """知识块表:文本片段 + 向量,向量检索的载体。

    不继承 TimestampMixin:chunk 是文档切片的产物,生命周期随文档,
    无独立更新语义,created_at 也无必要(按 chunk_index 排序即可)。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # ondelete=CASCADE:删文档连带删其所有向量块。
    doc_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_docs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属文档",
    )
    # chunk_index:块在文档内的顺序,用于回溯拼接上下文。
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="块在文档内的序号"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="块文本内容"
    )
    # embedding:pgvector 向量列。Vector(N) 在 DDL 生成 vector(N)。
    # nullable=True 容忍"先入库文本、异步回填向量"的流水线;
    # 检索时 WHERE embedding IS NOT NULL 过滤未向量化的块。
    embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(EMBEDDING_DIMENSION),
        nullable=True,
        comment="文本向量(1024 维,余弦检索)",
    )
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="块元数据(起止字符位置/来源页码等)",
    )

    # ---- relationships ----
    doc: Mapped["KnowledgeDoc"] = relationship(back_populates="chunks")

    # ---- 索引 ----
    # HNSW 向量索引:pgvector 提供的近似最近邻索引,查询延迟远低于精确扫描。
    # vector_cosine_ops:余弦距离操作符类,与检索时用的 <=> 算子匹配;
    # 必须与查询算子一致,否则索引不会被命中。
    # 复合普通索引(doc_id, chunk_index):便于按文档取全部块并按序拼接。
    __table_args__ = (
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_knowledge_chunks_doc_index",
            "doc_id",
            "chunk_index",
        ),
    )
