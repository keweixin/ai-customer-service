"""知识库 DTO:文档上传 / 响应 / 切片 / 检索请求。

对齐 API 层调用契约(knowledge.py):
- ``DocumentUpload`` -- POST /knowledge/documents 请求体(title, content, source_type)
- ``DocumentResponse.model_validate(document, from_attributes=True)`` -- 从 ORM KnowledgeDoc
- ``ChunkResponse.model_validate(chunk, from_attributes=True)`` -- 从检索结果 dict
  (含 content/score/doc_title/doc_id/chunk_id/chunk_index)
- ``DocumentListResponse(documents=[...])`` -- 文档列表
- ``SearchRequest`` -- POST /knowledge/search 请求体(query)

source_type 用字符串而非枚举:模型层是枚举,API 层传字符串更自然,
RagService 内部归一化为枚举;schema 层用 Literal 约束取值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentUpload(BaseModel):
    """文档上传请求体。

    content 为原始全文,服务端负责切块与向量化。
    """

    title: str = Field(..., min_length=1, max_length=512, description="文档标题")
    content: str = Field(..., min_length=1, description="文档原始内容(全文)")
    source_type: Literal["file", "url", "text"] = Field(
        default="text", description="来源类型(file/url/text)"
    )
    metadata: dict | None = Field(default=None, description="可选元信息")

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, v: str) -> str:
        """拒绝纯空白内容,避免切出 0 块。"""
        if not v.strip():
            raise ValueError("文档内容不能为空或纯空白")
        return v

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "title": "退换货政策",
            "content": "1. 7天内可无理由退换...\n\n2. 商品需保持原包装...",
            "source_type": "text",
        }
    })


class DocumentResponse(BaseModel):
    """文档响应(元信息,不含向量/切片内容)。"""

    id: UUID = Field(..., description="文档 ID")
    title: str = Field(..., description="文档标题")
    source_type: str = Field(..., description="来源类型")
    chunks_count: int = Field(..., description="切块数量")
    created_at: datetime = Field(..., description="创建时间")

    model_config = ConfigDict(from_attributes=True)

    @field_validator("source_type")
    @classmethod
    def _source_to_str(cls, v: object) -> str:
        """SourceType 枚举转字符串。"""
        return getattr(v, "value", str(v))


class ChunkResponse(BaseModel):
    """检索切片响应。

    可从 ORM KnowledgeChunk 序列化,也可从 retrieve() 返回的 dict 序列化
    (dict 含 content/score/doc_title/doc_id/chunk_id/chunk_index)。
    from_attributes=True 同时兼容对象属性与字典键访问(dict 也算 mapping)。
    """

    content: str = Field(..., description="切片文本")
    score: float | None = Field(default=None, description="相似度分数(0-1,1 最佳;关键词检索为空)")
    doc_title: str | None = Field(default=None, description="所属文档标题")
    doc_id: UUID | None = Field(default=None, description="所属文档 ID")
    chunk_id: UUID | None = Field(default=None, description="切片 ID")
    chunk_index: int | None = Field(default=None, description="切片序号")

    model_config = ConfigDict(from_attributes=True)


class DocumentListResponse(BaseModel):
    """文档列表响应(构造方式:DocumentListResponse(documents=[...]))。"""

    documents: list[DocumentResponse] = Field(
        default_factory=list, description="文档列表"
    )


class SearchRequest(BaseModel):
    """检索测试请求体。"""

    query: str = Field(..., min_length=1, max_length=2000, description="检索 query")

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("检索 query 不能为空")
        return v

    model_config = ConfigDict(json_schema_extra={
        "example": {"query": "退货流程是怎样的"}
    })
