"""安全模块单元测试。

被测模块: ``app.core.security``

覆盖:
- 密码哈希与校验(hash_password / verify_password)。
- JWT 签发与解码(create_access_token / decode_access_token)。
- 无效 token / 过期 token / 缺 sub 的 token 应抛 AuthenticationError。
- 不同密码不匹配,损坏的哈希不抛异常而返回 False。
"""

from __future__ import annotations

from datetime import timedelta

import pytest


class TestPasswordHashing:
    """hash_password / verify_password 行为。"""

    def test_hash_and_verify_password(self) -> None:
        """正确明文应通过校验,且哈希不等于明文。"""
        from app.core.security import hash_password, verify_password

        plain = "MyP@ssw0rd-2024"
        hashed = hash_password(plain)

        assert isinstance(hashed, str)
        assert hashed != plain, "哈希不应等于明文"
        assert hashed.startswith("$2"), "bcrypt 哈希应以 $2 开头"
        assert verify_password(plain, hashed) is True

    def test_verify_password_wrong_password(self) -> None:
        """错误明文校验应返回 False(不抛异常)。"""
        from app.core.security import hash_password, verify_password

        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False

    def test_hash_is_random_per_call(self) -> None:
        """同一明文两次哈希结果不同(bcrypt 自带 salt)。"""
        from app.core.security import hash_password

        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2, "bcrypt 应每次生成不同 salt"
        # 但两者都能校验通过
        from app.core.security import verify_password

        assert verify_password("same-password", h1)
        assert verify_password("same-password", h2)

    def test_verify_password_corrupt_hash_returns_false(self) -> None:
        """损坏的哈希字符串应返回 False 而非抛异常。"""
        from app.core.security import verify_password

        assert verify_password("anything", "not-a-valid-hash") is False
        assert verify_password("anything", "") is False


class TestCreateAccessToken:
    """create_access_token 行为。"""

    def test_create_and_decode_access_token(self) -> None:
        """签发的 token 能被正确解码,payload 含 sub 与 exp。"""
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token({"sub": "user-123", "role": "user"})
        assert isinstance(token, str)
        assert token.count(".") == 2, "JWT 应为三段式 header.payload.signature"

        payload = decode_access_token(token)
        assert payload["sub"] == "user-123"
        assert payload["role"] == "user"
        assert "exp" in payload, "payload 应含过期时间"

    def test_create_token_with_custom_expiry(self) -> None:
        """自定义 expires_delta 应反映在 exp 中。"""
        from datetime import datetime, timezone

        from app.core.security import create_access_token, decode_access_token

        before = datetime.now(timezone.utc)
        token = create_access_token(
            {"sub": "u1"}, expires_delta=timedelta(hours=2)
        )
        payload = decode_access_token(token)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        # exp 应在 (before+2h-10s, before+2h+10s) 区间,留容差
        assert before + timedelta(hours=2, minutes=-1) <= exp <= before + timedelta(
            hours=2, minutes=1
        )

    def test_create_token_without_sub_raises(self) -> None:
        """缺 sub 字段应抛 AuthenticationError(AUTH_002)。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import create_access_token

        with pytest.raises(AuthenticationError) as exc_info:
            create_access_token({"role": "user"})  # 无 sub
        assert exc_info.value.error_code == "AUTH_002"

    def test_token_does_not_mutate_input_dict(self) -> None:
        """create_access_token 不应修改调用方传入的字典。"""
        from app.core.security import create_access_token

        data = {"sub": "u1", "role": "user"}
        original = dict(data)
        create_access_token(data)
        assert data == original, "入参字典不应被修改"


class TestDecodeAccessToken:
    """decode_access_token 错误处理。"""

    def test_decode_invalid_token_raises(self) -> None:
        """格式非法的 token 应抛 AuthenticationError(AUTH_004)。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import decode_access_token

        with pytest.raises(AuthenticationError) as exc_info:
            decode_access_token("not.a.valid.jwt.token")
        assert exc_info.value.error_code == "AUTH_004"

    def test_decode_garbage_string_raises(self) -> None:
        """完全非 token 的字符串应抛 AuthenticationError。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import decode_access_token

        with pytest.raises(AuthenticationError):
            decode_access_token("garbage")

    def test_decode_empty_string_raises(self) -> None:
        """空字符串应抛 AuthenticationError。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import decode_access_token

        with pytest.raises(AuthenticationError):
            decode_access_token("")

    def test_token_expired_raises(self) -> None:
        """过期 token 应抛 AuthenticationError(AUTH_003)。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token(
            {"sub": "u1"}, expires_delta=timedelta(seconds=-1)
        )
        with pytest.raises(AuthenticationError) as exc_info:
            decode_access_token(token)
        assert exc_info.value.error_code == "AUTH_003"

    def test_token_with_wrong_secret_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用不同 secret 签发的 token 解码应失败(AUTH_004)。"""
        from app.core.exceptions import AuthenticationError
        from app.core.security import create_access_token, decode_access_token

        # 用一个 secret 签发
        monkeypatch.setenv("JWT_SECRET_KEY", "secret-A")
        from app.config import reset_settings

        reset_settings()
        token = create_access_token({"sub": "u1"})

        # 换 secret 再解码
        monkeypatch.setenv("JWT_SECRET_KEY", "secret-B-different")
        reset_settings()
        with pytest.raises(AuthenticationError) as exc_info:
            decode_access_token(token)
        assert exc_info.value.error_code == "AUTH_004"

    def test_token_missing_sub_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 解码后无 sub 字段应抛 AUTH_005。

        构造一个合法签名但 payload 无 sub 的 token:
        直接用 jwt.encode 跳过 create_access_token 的 sub 校验。
        """
        import jwt

        from app.config import get_settings
        from app.core.exceptions import AuthenticationError
        from app.core.security import decode_access_token

        settings = get_settings()
        bad_token = jwt.encode(
            {"role": "user", "exp": 9999999999},
            settings.jwt.secret_key.get_secret_value(),
            algorithm=settings.jwt.algorithm,
        )
        with pytest.raises(AuthenticationError) as exc_info:
            decode_access_token(bad_token)
        assert exc_info.value.error_code == "AUTH_005"


class TestRoundtrip:
    """端到端往返测试:签发 -> 解码 一致。"""

    def test_roundtrip_preserves_custom_claims(self) -> None:
        """自定义 claim(如 session_id)应在解码后保留。"""
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token(
            {"sub": "user-xyz", "role": "admin", "session_id": "sess-1"}
        )
        payload = decode_access_token(token)
        assert payload["sub"] == "user-xyz"
        assert payload["role"] == "admin"
        assert payload["session_id"] == "sess-1"
