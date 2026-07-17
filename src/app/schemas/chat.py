"""对话 DTO:请求 / 响应 / 消息 / 会话列表。

对齐 API 层调用契约(chat.py):
- ``ChatRequest`` -- POST /chat 请求体(session_id 可选,message 必填)
- ``ChatResponse`` -- 非流式回复(流式走 SSE,此 DTO 供非流式场景/测试用)
- ``MessageResponse`` -- 单条消息
- ``SessionListResponse(sessions=[...])`` -- 会话列表
- ``MessageListResponse(messages=[...])`` -- 消息列表
- ``SessionResponse.model_validate(session, from_attributes=True)`` -- 单会话

注意:ChatRequest.session_id 为 Optional[UUID],None 表示新建会话;
chat.py 据 None 分支新建会话。message 不能为空。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    """对话请求体。

    session_id 为空表示新建会话(服务端创建后通过 SSE done 事件回传 session_id)。
    """

    session_id: UUID | None = Field(
        default=None, description="会话 ID;为空则新建会话"
    )
    message: str = Field(
        ..., min_length=1, max_length=8000, description="用户消息(1-8000 字符)"
    )

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, v: str) -> str:
        """拒绝纯空白消息,避免无意义 LLM 调用。"""
        if not v.strip():
            raise ValueError("消息不能为空或纯空白")
        return v

    model_config = ConfigDict(json_schema_extra={
        "example": {"session_id": None, "message": "你好,我想查一下我的订单"}
    })


class ChatResponse(BaseModel):
    """非流式对话响应(流式场景走 SSE,此 DTO 用于非流式/测试)。

    usage 携带 token 用量,供前端展示成本或做软限流。
    """

    reply: str = Field(..., description="AI 回复文本")
    session_id: UUID = Field(..., description="会话 ID(新建时回传)")
    usage: dict[str, Any] = Field(
        default_factory=dict, description="token 用量(prompt_tokens/completion_tokens)"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "reply": "您好!请提供您的订单号,我帮您查询。",
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }
    })


class MessageResponse(BaseModel):
    """单条消息响应。"""

    role: str = Field(..., description="消息角色(user/assistant/system/tool)")
    content: str = Field(..., description="消息内容")
    created_at: datetime = Field(..., description="创建时间")

    model_config = ConfigDict(from_attributes=True)

    @field_validator("role")
    @classmethod
    def _role_to_str(cls, v: object) -> str:
        """MessageRole 枚举转字符串。"""
        return getattr(v, "value", str(v))


class SessionResponse(BaseModel):
    """会话响应(元信息,不含消息)。"""

    id: UUID = Field(..., description="会话 ID")
    user_id: UUID = Field(..., description="所属用户")
    status: str = Field(..., description="会话状态(active/closed/transferred)")
    started_at: datetime | None = Field(default=None, description="会话开始时间")
    ended_at: datetime | None = Field(default=None, description="会话结束时间")
    created_at: datetime = Field(..., description="创建时间")

    model_config = ConfigDict(from_attributes=True)

    @field_validator("status")
    @classmethod
    def _status_to_str(cls, v: object) -> str:
        """SessionStatus 枚举转字符串。"""
        return getattr(v, "value", str(v))


class SessionListResponse(BaseModel):
    """会话列表响应(构造方式:SessionListResponse(sessions=[...]))。"""

    sessions: list[SessionResponse] = Field(default_factory=list, description="会话列表")


class MessageListResponse(BaseModel):
    """消息列表响应(构造方式:MessageListResponse(messages=[...]))。"""

    messages: list[MessageResponse] = Field(
        default_factory=list, description="消息列表(按时间正序)"
    )
