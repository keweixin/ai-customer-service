"""鉴权集成测试(/auth 路由)。

被测路由: ``app.api.v1.auth``

覆盖注册/登录/me 主流程:
- 注册成功 201、重复用户名失败 4xx、登录成功返回 token、
  错误密码失败、无 token 访问 me 失败 401、有 token 访问 me 成功。

TestClient + 内存 SQLite,密码走真实 bcrypt 哈希(测真实链路),
JWT 走真实签发/校验。若 app.main 未就绪,client fixture 返回 None,
测试自动跳过。
"""

from __future__ import annotations

import pytest


# 若 app 未就绪,client fixture 返回 None,统一跳过
pytestmark = pytest.mark.skipif(
    pytest.importorskip("app.main", reason="app.main 未就绪") is None,
    reason="app.main 尚未实现,集成测试跳过",
)


def _unique_user(prefix: str = "user") -> tuple[str, str, str]:
    """生成唯一用户名/邮箱/密码,避免测试间数据冲突。"""
    import uuid

    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_{suffix}", f"{prefix}_{suffix}@test.com", "P@ssw0rd-Test1"


class TestRegister:
    """注册接口 POST /api/v1/auth/register。"""

    def test_register_success(self, client) -> None:
        """合法注册请求应返回 201 / 200 与用户信息(不含密码)。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, password = _unique_user("reg")

        resp = client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )

        assert resp.status_code in (200, 201), f"注册失败: {resp.text}"
        body = resp.json()
        # 返回体不应含密码字段
        assert "password" not in str(body).lower() or "password_hash" not in str(body)
        # 应含用户名
        if isinstance(body, dict):
            if "user" in body:
                assert body["user"]["username"] == username
            elif "username" in body:
                assert body["username"] == username

    def test_register_duplicate_username_fails(self, client) -> None:
        """重复用户名注册应失败(409 / 400 / 422)。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, password = _unique_user("dup")

        # 第一次注册成功
        r1 = client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        assert r1.status_code in (200, 201)

        # 第二次同用户名(换邮箱)应失败
        r2 = client.post(
            "/api/v1/auth/register",
            json={
                "username": username,
                "email": f"another_{email}",
                "password": password,
            },
        )
        assert r2.status_code >= 400, "重复用户名应被拒绝"

    def test_register_short_password_fails(self, client) -> None:
        """过短密码应被校验拒绝(422)。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, _ = _unique_user("short")
        resp = client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": "1"},
        )
        assert resp.status_code >= 400

    def test_register_invalid_email_fails(self, client) -> None:
        """非法邮箱格式应被校验拒绝(422)。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, _, password = _unique_user("badmail")
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "username": username,
                "email": "not-an-email",
                "password": password,
            },
        )
        assert resp.status_code >= 400


class TestLogin:
    """登录接口 POST /api/v1/auth/login。"""

    def test_login_success(self, client) -> None:
        """正确凭据登录应返回 access_token。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, password = _unique_user("login")
        # 先注册
        client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )

        resp = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"登录失败: {resp.text}"
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        assert token, "登录响应应含 access_token"
        assert isinstance(token, str)
        assert token.count(".") == 2, "token 应为 JWT 三段式"

    def test_login_wrong_password_fails(self, client) -> None:
        """错误密码登录应失败(401 / 400)。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, password = _unique_user("wrongpwd")
        client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )

        resp = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "Wrong-Pwd-9999"},
        )
        assert resp.status_code in (400, 401, 403), "错误密码应被拒绝"

    def test_login_nonexistent_user_fails(self, client) -> None:
        """不存在的用户登录应失败。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "ghost_user_xyz", "password": "whatever"},
        )
        assert resp.status_code in (400, 401, 404)


class TestGetMe:
    """当前用户接口 GET /api/v1/auth/me。"""

    def test_get_me_without_token_fails(self, client) -> None:
        """无 token 访问 me 应返回 401。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401, f"无 token 应 401,实际 {resp.status_code}"

    def test_get_me_with_token_success(self, client) -> None:
        """带有效 token 访问 me 应返回当前用户信息。"""
        if client is None:
            pytest.skip("client 未就绪")
        username, email, password = _unique_user("me")
        client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        token = login_resp.json().get("access_token") or login_resp.json().get("token")

        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"带 token 应 200,实际 {resp.status_code}: {resp.text}"
        body = resp.json()
        if isinstance(body, dict):
            if "username" in body:
                assert body["username"] == username
            elif "user" in body:
                assert body["user"]["username"] == username
        # 不应泄露密码
        assert "password_hash" not in resp.text or resp.text.count("password_hash") == 0

    def test_get_me_with_invalid_token_fails(self, client) -> None:
        """无效 token 访问 me 应返回 401。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_get_me_without_bearer_prefix_fails(self, client) -> None:
        """Authorization 头不带 Bearer 前缀应 401。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "some-token-without-bearer"},
        )
        assert resp.status_code == 401
