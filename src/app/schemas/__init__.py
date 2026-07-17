"""Pydantic schemas(DTO):API 请求/响应的数据契约。

按业务域组织:
- common: 通用响应(错误/成功/分页)
- auth: 鉴权(注册/登录/Token/用户)
- chat: 对话(请求/响应/消息/会话)
- knowledge: 知识库(文档/切片/检索)
- admin: 管理后台(统计/审计/用户列表)

统一约定:
- Pydantic v2 风格:ConfigDict(from_attributes=True) 兼容 ORM;
- 枚举字段用 field_validator 转 .value 字符串,便于 JSON 序列化;
- 全部字段类型注解,可选字段用 ``X | None``;
- 响应包装类(SessionListResponse 等)用 list 字段而非继承,构造更直观。
"""

from __future__ import annotations

from app.schemas.admin import (
    AuditLogListResponse,
    AuditLogResponse,
    StatsResponse,
    UserListResponse,
)
from app.schemas.auth import (
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    MessageListResponse,
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)
from app.schemas.common import (
    ErrorResponse,
    PaginationResponse,
    SuccessResponse,
)
from app.schemas.knowledge import (
    ChunkResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentUpload,
    SearchRequest,
)

__all__ = [
    # common
    "ErrorResponse",
    "SuccessResponse",
    "PaginationResponse",
    # auth
    "UserCreate",
    "UserLogin",
    "TokenResponse",
    "UserResponse",
    # chat
    "ChatRequest",
    "ChatResponse",
    "MessageResponse",
    "SessionResponse",
    "SessionListResponse",
    "MessageListResponse",
    # knowledge
    "DocumentUpload",
    "DocumentResponse",
    "ChunkResponse",
    "DocumentListResponse",
    "SearchRequest",
    # admin
    "StatsResponse",
    "AuditLogResponse",
    "AuditLogListResponse",
    "UserListResponse",
]
