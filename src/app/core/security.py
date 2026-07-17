"""安全模块:JWT 签发/校验与密码哈希。

- JWT 用于无状态鉴权,payload 含 sub(user_id) / role / exp。
- 密码使用 passlib + bcrypt 哈希存储,验证时只比对哈希。
- 所有密钥/算法/过期时间从 config 读取,避免硬编码。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.core.exceptions import AuthenticationError

# passlib 上下文:仅启用 bcrypt,deprecated 自动迁移关闭以减少噪音
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(
    data: dict[str, Any],
    *,
    expires_delta: timedelta | None = None,
) -> str:
    """签发 JWT access token。

    Args:
        data: 业务负载,应包含 ``sub``(user_id)与 ``role``。
        expires_delta: 自定义有效期;不传则使用配置默认值。

    Returns:
        编码后的 JWT 字符串。
    """
    settings = get_settings()
    jwt_cfg = settings.jwt

    # 复制入参避免修改调用方字典
    to_encode = dict(data)

    # sub 是 OpenID Connect 约定的 subject 字段,放用户唯一标识
    if "sub" not in to_encode:
        raise AuthenticationError(
            "创建 token 缺少 sub(user_id)字段", error_code="AUTH_002"
        )

    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=jwt_cfg.access_token_expire_minutes)
    )
    to_encode["exp"] = expire

    return jwt.encode(
        to_encode,
        jwt_cfg.secret_key.get_secret_value(),
        algorithm=jwt_cfg.algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解码 JWT。

    Args:
        token: 客户端传入的 Bearer token。

    Returns:
        解码后的 payload 字典。

    Raises:
        AuthenticationError: token 过期、签名无效、格式错误等任何校验失败。
    """
    settings = get_settings()
    jwt_cfg = settings.jwt

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            jwt_cfg.secret_key.get_secret_value(),
            algorithms=[jwt_cfg.algorithm],
        )
    except jwt.ExpiredSignatureError:
        # 过期单独提示,前端据此引导重新登录
        raise AuthenticationError("登录已过期,请重新登录", error_code="AUTH_003")
    except jwt.InvalidTokenError:
        # 其他无效原因统一为认证失败,不暴露具体差异防止枚举攻击
        raise AuthenticationError("认证凭据无效", error_code="AUTH_004")

    if "sub" not in payload:
        # 防御性校验:无 sub 的 token 视为非法
        raise AuthenticationError("token 缺少 subject", error_code="AUTH_005")

    return payload


def hash_password(plain: str) -> str:
    """对明文密码做 bcrypt 哈希。

    Args:
        plain: 明文密码。

    Returns:
        哈希字符串(含 salt 与算法标识),直接存库。
    """
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。

    使用常量时间比较(passlib 内部实现),防止时序侧信道攻击。

    Args:
        plain: 用户输入的明文密码。
        hashed: 库中存储的哈希字符串。

    Returns:
        匹配返回 True,否则 False。不抛异常以简化调用方逻辑。
    """
    try:
        return _pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        # 哈希格式损坏等情况不抛异常,统一返回 False
        return False
