"""知识库路由:文档上传 / 列表 / 删除 / 检索测试。

设计要点:
- 文档管理(上传/删除)仅 admin 可用,普通用户只读--防止知识库被污染。
- 检索测试接口 ``/knowledge/search`` 对所有登录用户开放,便于前端调试
  与"猜你想问"类功能复用。
- 删除采用软删除还是硬删除取决于业务策略,这里调 RagService 统一处理,
  路由层不关心存储细节,保持关注点分离。
- 文档上传涉及切块 + 向量化,耗时较长,内部由 RagService 异步处理;
  路由层只做参数校验与权限控制。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_admin,
    get_current_user,
    get_db,
    get_rag_service,
)
from app.core.logging import get_logger
from app.models.user import User
from app.schemas.knowledge import ChunkResponse, DocumentListResponse, DocumentResponse, DocumentUpload, SearchRequest

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    summary="上传知识库文档",
)
async def upload_document(
    payload: DocumentUpload,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    rag_service=Depends(get_rag_service),
) -> None:
    """上传文档到知识库(admin only)。

    RagService 负责:文本抽取 -> 切块 -> embedding -> 落库(含向量)。
    返回 DocumentResponse,前端据此展示上传结果与切片数。
    """
    document = await rag_service.upload_document(payload)
    await db.commit()

    logger.info(
        "文档上传成功",
        document_id=str(document.id),  # type: ignore[attr-defined]
        title=getattr(payload, "title", None),
        admin_id=str(admin.id),
    )
    return DocumentResponse.model_validate(document, from_attributes=True)


@router.get(
    "/documents",
    summary="列出知识库文档",
)
async def list_documents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> None:
    """分页列出知识库文档(所有登录用户可读)。

    只返回文档元信息,不返回向量/切片内容,避免响应过大。
    """
    from app.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(db)
    documents = await repo.list_all(limit=limit, offset=offset)
    return DocumentListResponse(documents=documents)  # type: ignore[call-arg]


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_200_OK,
    summary="删除知识库文档",
)
async def delete_document(
    document_id: UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    rag_service=Depends(get_rag_service),
) -> dict:
    """删除文档及其所有切片与向量(admin only)。

    幂等:删除不存在的文档返回 404(而非静默成功),便于前端排查。
    """
    from app.core.exceptions import NotFoundError

    deleted = await rag_service.delete_document(document_id)
    if not deleted:
        raise NotFoundError("文档不存在")
    await db.commit()

    logger.info(
        "文档已删除", document_id=str(document_id), admin_id=str(admin.id)
    )
    return {"deleted": True, "document_id": str(document_id)}


@router.post(
    "/search",
    summary="检索测试(返回 top-k 切片)",
)
async def search(
    payload: SearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    rag_service=Depends(get_rag_service),
) -> None:
    """检索测试接口:输入 query,返回相关切片列表。

    供前端"猜你想问"或管理员验证知识库召回质量使用。
    检索参数(top_k / min_similarity)走 RagService 默认值,即 config.rag。
    """
    chunks = await rag_service.retrieve(query=payload.query)
    # 序列化每个切片;model_validate 兼容 ORM 对象
    result = [
        ChunkResponse.model_validate(chunk, from_attributes=True) for chunk in chunks
    ]
    logger.info(
        "检索测试",
        query=payload.query,
        hit_count=len(result),
        user_id=str(user.id),
    )
    return result
