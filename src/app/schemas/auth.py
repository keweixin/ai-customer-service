"""鉴权 DTO:注册 / 登录 / Token / 用户信息。

对齐 API 层调用契约(auth.py):
- ``UserCreate`` -- 注册请求体(username, password, email)
- ``UserLogin`` -- 登录请求体(username, password)
- ``TokenResponse(access_token=, token_type=)`` -- 登录成功返回
- ``UserResponse.model_validate(user, from_attributes=True)`` -- 从 ORM User 序列化

Pydantic v2 风格:ConfigDict(from_attributes=True) 兼容 ORM;field_validator
做密码强度与邮箱格式校验。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreate(BaseModel):
    """注册新用户请求体。"""

    username: str = Field(
        ..., min_length=3, max_length=64, description="登录用户名(3-64 字符)"
    )
    password: str = Field(
        ..., min_length=6, max_length=128, description="明文密码(6-128 字符,哈希后存库)"
    )
    # 用 str + 正则校验而非 EmailStr:EmailStr 依赖额外的 email-validator 包,
    # pyproject.toml 未声明该依赖;用内置正则保持零额外依赖,覆盖常见邮箱格式。
    email: str = Field(..., max_length=255, description="邮箱(唯一)")

    @field_validator("username")
    @classmethod
    def _username_no_whitespace(cls, v: str) -> str:
        """用户名不含空白,防止登录态解析歧义。"""
        if any(ch.isspace() for ch in v):
            raise ValueError("用户名不能包含空白字符")
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        """基础强度校验:至少含字母与数字;复杂场景应叠加 bcrypt 之外的正则。"""
        if not any(ch.isalpha() for ch in v) or not any(ch.isdigit() for ch in v):
            raise ValueError("密码需同时包含字母与数字")
        return v

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str) -> str:
        """简易邮箱格式校验(含 @ 与域名点),避免引入 email-validator 依赖。

        不追求 RFC 完整性:覆盖 user@host.tld 常见格式即可,严格校验由 DB 唯一约束
        与实际发信兜底。
        """
        v = v.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("邮箱格式不正确")
        local, _, domain = v.rpartition("@")
        if "." not in domain or not local:
            raise ValueError("邮箱格式不正确")
        return v

    model_config = ConfigDict(json_schema_extra={
        "example": {"username": "alice", "password": "alice123", "email": "alice@example.com"}
    })


class UserLogin(BaseModel):
    """登录请求体。"""

    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, max_length=128, description="明文密码")

    model_config = ConfigDict(json_schema_extra={
        "example": {"username": "alice", "password": "alice123"}
    })


class TokenResponse(BaseModel):
    """登录成功返回的 JWT token。"""

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="token 类型,固定 bearer")
    expires_in: int | None = Field(
        default=None, description="token 有效期(秒),可选"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {"access_token": "eyJhbGciOi...", "token_type": "bearer"}
    })


class UserResponse(BaseModel):
    """用户信息响应(不含 password_hash)。

    from_attributes=True 使其能直接从 ORM User 对象 model_validate。
    """

    id: UUID = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    email: str = Field(..., description="邮箱")
    role: str = Field(..., description="角色(user/admin)")
    is_active: bool = Field(default=True, description="是否启用")
    created_at: datetime = Field(..., description="创建时间")

    model_config = ConfigDict(from_attributes=True)

    @field_validator("role")
    @classmethod
    def _role_to_str(cls, v: object) -> str:
        """UserRole 枚举转字符串,便于 JSON 序列化。"""
        # ORM 取出的 role 是 UserRole 枚举,其 .value 即字符串;str 兜底。
        return getattr(v, "value", str(v))
