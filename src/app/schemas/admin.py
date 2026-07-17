"""管理后台 DTO:统计看板 / 审计日志 / 用户列表。

对齐 API 层调用契约(admin.py):
- ``StatsResponse(users=, sessions=, messages=, documents=, today_llm_calls=)``
- ``AuditLogListResponse(logs=[...])`` -- logs 为 AuditLog ORM 对象列表
- ``UserListResponse(users=[...])`` -- users 为 User ORM 对象列表

AuditLogResponse / 用户复用 auth.UserResponse(同字段集),避免重复定义;
此处单独定义 AuditLogResponse 因审计日志字段(user_id/action/target/detail)
与用户不同。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.auth import UserResponse


class StatsResponse(BaseModel):
    """统计看板响应。

    各字段为对应表的行数;today_llm_calls 为今日(UTC 0 点起)以 ``llm.`` 开头的
    审计动作计数,用于监控 LLM 调用量与成本。
    """

    users: int = Field(default=0, ge=0, description="用户总数")
    sessions: int = Field(default=0, ge=0, description="会话总数")
    messages: int = Field(default=0, ge=0, description="消息总数")
    documents: int = Field(default=0, ge=0, description="知识库文档总数")
    today_llm_calls: int = Field(default=0, ge=0, description="今日 LLM 调用次数")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "users": 128, "sessions": 1024, "messages": 8192,
            "documents": 32, "today_llm_calls": 256,
        }
    })


class AuditLogResponse(BaseModel):
    """审计日志响应(从 ORM AuditLog 序列化)。"""

    id: UUID = Field(..., description="审计日志 ID")
    user_id: UUID | None = Field(default=None, description="操作者(系统操作为空)")
    action: str = Field(..., description="动作标识(点分字符串)")
    target: str | None = Field(default=None, description="动作目标")
    detail: dict[str, Any] | None = Field(default=None, description="动作详情")
    ip_address: str | None = Field(default=None, description="操作来源 IP")
    message: str | None = Field(default=None, description="结果摘要")
    created_at: datetime = Field(..., description="动作发生时间")

    model_config = ConfigDict(from_attributes=True)


class AuditLogListResponse(BaseModel):
    """审计日志列表响应(构造方式:AuditLogListResponse(logs=[...]))。"""

    logs: list[AuditLogResponse] = Field(default_factory=list, description="审计日志列表")


class UserListResponse(BaseModel):
    """用户列表响应(构造方式:UserListResponse(users=[...]))。

    复用 auth.UserResponse,字段集一致(id/username/email/role/created_at)。
    """

    users: list[UserResponse] = Field(default_factory=list, description="用户列表")
