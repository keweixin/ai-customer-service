"""通用 DTO:错误响应、分页响应、成功响应。

统一响应体约定:
- 错误:``{"error": {"code": "...", "message": "..."}}``(见 core.exceptions);
- 成功(可选包装):``{"message": "...", "data": {...}}``;
- 分页:``{"items": [...], "total": N, "page": P, "page_size": S}``。

Pydantic v2 风格:
- ``model_config = ConfigDict(from_attributes=True)`` 兼容 ORM 对象;
- 校验用 ``field_validator`` / ``model_validator``;
- 全部字段类型注解,可选字段用 ``X | None``。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorResponse(BaseModel):
    """统一错误响应体。

    结构与 core.exceptions._error_response 输出对齐,前端按 error.code 分支处理。
    """

    error: dict[str, Any] = Field(
        ...,
        description="错误详情,含 code(业务错误码)与 message(可读信息)",
    )


class SuccessResponse(BaseModel):
    """通用成功响应包装(可选,多数接口直接返回业务对象)。"""

    message: str = Field(default="ok", description="结果描述")
    data: Any | None = Field(default=None, description="业务数据")


class PaginationResponse(BaseModel, Generic[T]):
    """分页响应(泛型,items 类型由调用方指定)。

    用法:
        PaginationResponse[DocumentResponse](items=[...], total=100, page=1, page_size=20)
    """

    items: list[T] = Field(default_factory=list, description="当前页数据")
    total: int = Field(default=0, ge=0, description="总记录数")
    page: int = Field(default=1, ge=1, description="当前页码(1-based)")
    page_size: int = Field(default=20, ge=1, description="每页条数")

    model_config = ConfigDict(from_attributes=True)
