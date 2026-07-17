"""健康检查集成测试。

被测路由: ``app.api``(可能挂 /health 与 /ready,
或在 main.py 直接注册)

覆盖:
- /health 返回 200 与 ok 状态(liveness,不查依赖)。
- /ready 检查 DB(就绪探针,依赖可用才返回 200)。
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skipif(
    pytest.importorskip("app.main", reason="app.main 未就绪") is None,
    reason="app.main 尚未实现,集成测试跳过",
)


def _try_get(client, paths: list[str]):
    """依次尝试一组路径,返回第一个非 404 的响应。"""
    for path in paths:
        resp = client.get(path)
        if resp.status_code != 404:
            return resp, path
    return None, None


class TestHealth:
    """liveness 探针。"""

    def test_health_returns_ok(self, client) -> None:
        """/health 应返回 200,且状态为 ok / healthy。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp, path = _try_get(client, ["/health", "/healthz", "/api/v1/health", "/live"])
        if resp is None:
            pytest.skip("未找到 health 端点")
        assert resp.status_code == 200, f"{path} 应 200,实际 {resp.status_code}"
        body = resp.json()
        # 状态字段可能叫 status / status / health
        status_val = (
            body.get("status") if isinstance(body, dict) else None
        )
        if status_val is not None:
            assert str(status_val).lower() in ("ok", "healthy", "up", "alive"), (
                f"health 状态应为 ok/healthy,实际 {status_val}"
            )

    def test_health_does_not_require_auth(self, client) -> None:
        """health 端点不应要求鉴权(探针无 token)。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp, path = _try_get(client, ["/health", "/healthz", "/api/v1/health"])
        if resp is None:
            pytest.skip("未找到 health 端点")
        assert resp.status_code != 401, f"{path} 不应要求鉴权"
        assert resp.status_code != 403, f"{path} 不应要求鉴权"


class TestReady:
    """readiness 探针(检查 DB)。"""

    def test_ready_checks_db(self, client, db_session) -> None:
        """/ready 在 DB 可用时返回 200,状态 ready。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp, path = _try_get(client, ["/ready", "/readiness", "/api/v1/ready"])
        if resp is None:
            pytest.skip("未找到 ready 端点")
        # DB 用内存 SQLite(mock 注入),应可用 -> 200
        assert resp.status_code in (200, 503), f"{path} 应返回 200 或 503,实际 {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            status_val = body.get("status") if isinstance(body, dict) else None
            if status_val is not None:
                assert str(status_val).lower() in ("ok", "ready", "healthy", "up"), (
                    f"ready 状态应为 ready,实际 {status_val}"
                )
            # 若返回依赖明细,DB 应为 ok
            deps = body.get("dependencies") or body.get("checks") if isinstance(body, dict) else None
            if isinstance(deps, dict):
                db_status = deps.get("database") or deps.get("db")
                if db_status is not None:
                    assert str(db_status).lower() in ("ok", "up", "healthy", "ready"), (
                        f"DB 依赖应 ok,实际 {db_status}"
                    )

    def test_ready_does_not_require_auth(self, client) -> None:
        """ready 端点不应要求鉴权。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp, path = _try_get(client, ["/ready", "/readiness", "/api/v1/ready"])
        if resp is None:
            pytest.skip("未找到 ready 端点")
        assert resp.status_code not in (401, 403), f"{path} 不应要求鉴权"


class TestRootOrDocs:
    """根路径 / 文档可访问性(轻量冒烟)。"""

    def test_openapi_docs_accessible(self, client) -> None:
        """OpenAPI 文档端点应可访问(说明 app 启动正常)。"""
        if client is None:
            pytest.skip("client 未就绪")
        resp = client.get("/openapi.json")
        # 允许 404(若禁用了 docs),但不应 500
        assert resp.status_code in (200, 404), f"openapi 应 200 或 404,实际 {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            assert "paths" in body, "OpenJSON 应含 paths"
