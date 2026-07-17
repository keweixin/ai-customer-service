"""知识库文档模型。

RAG 知识库的"文档级"元数据:一篇文档(file/url/text)切分成多个 chunk,
chunk 存向量。文档表本身不存向量,只存来源、切片数量、原始内容与元数据。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import UUID

from sqlalchemy import Enum, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .knowledge_chunk import KnowledgeChunk


class SourceType(str, enum.Enum):
    """知识来源类型。"""

    FILE = "file"  # 上传文件(pdf/docx/md...)
    URL = "url"    # 网页链接抓取
    TEXT = "text"  # 直接粘贴文本


class KnowledgeDoc(Base, TimestampMixin):
    """知识库文档表:一篇可被检索的知识来源。"""

    __tablename__ = "knowledge_docs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="文档标题"
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="来源类型",
    )
    # 原始全文:用于回溯展示与重新切片,Text 无长度上限。
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="文档原始内容")
    # chunks_count:冗余计数字段,避免每次列表展示都要 count(*) chunks;
    # 由应用层在切片完成后回写,不依赖触发器(切片是批处理动作)。
    chunks_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="切分后的块数量(冗余计数,便于列表展示)",
    )
    # metadata:存放文件名/MIME/上传者/哈希等溯源信息。
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="文档元数据(文件名/上传者/哈希等)",
    )

    # ---- relationships ----
    # 一对多:一篇文档多个 chunk。cascade all, delete-orphan,
    # 删文档连带删其所有向量块,防止向量指向不存在的文档。
    chunks: Mapped[List["KnowledgeChunk"]] = relationship(
        back_populates="doc",
        cascade="all, delete-orphan",
        order_by="KnowledgeChunk.chunk_index",
        lazy="selectin",
    )
