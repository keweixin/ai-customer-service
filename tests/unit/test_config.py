"""配置模块单元测试。

被测模块: ``app.config``

覆盖:
- BaseSettings 能从环境变量读取配置(env_prefix / alias 机制)。
- 必填字段缺失时聚合校验(validate_production)报错。
- 敏感字段(SecretStr)不直接暴露明文,repr / str 安全。
- RAG 配置 overlap >= chunk_size 报错(防死循环校验)。
- CORS 逗号分隔字符串解析为列表。
- get_settings 单例与 reset_settings 缓存清除。
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestAppConfig:
    """AppConfig 从 APP_ 前缀环境变量读取。"""

    def test_app_config_reads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """APP_ 前缀的环境变量应被 AppConfig 正确读取。"""
        monkeypatch.setenv("APP_NAME", "Test Service")
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("APP_PORT", "9000")
        monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")

        from app.config import AppConfig, reset_settings

        reset_settings()
        cfg = AppConfig()

        assert cfg.name == "Test Service"
        assert cfg.env == "staging"
        assert cfg.port == 9000
        assert cfg.log_level == "DEBUG"

    def test_app_config_defaults_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无环境变量时使用代码中的默认值。"""
        # 清掉 conftest 注入的 APP_ 系列变量,验证代码默认值
        for var in ("APP_ENV", "APP_PORT", "APP_DEBUG", "APP_NAME", "APP_LOG_LEVEL"):
            monkeypatch.delenv(var, raising=False)

        from app.config import AppConfig, reset_settings

        reset_settings()
        cfg = AppConfig()

        assert cfg.env == "development"
        assert cfg.port == 8000
        assert cfg.debug is False

    def test_is_production_property(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_production 在 env=production 时为 True,其余为 False。"""
        from app.config import AppConfig, reset_settings

        reset_settings()
        assert AppConfig(env="development").is_production is False  # type: ignore[call-arg]
        assert AppConfig(env="staging").is_production is False  # type: ignore[call-arg]
        assert AppConfig(env="production").is_production is True  # type: ignore[call-arg]

    def test_invalid_env_raises_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APP_ENV 给非枚举值时 pydantic 应拒绝。"""
        monkeypatch.setenv("APP_ENV", "qa")
        from app.config import AppConfig, reset_settings

        reset_settings()
        with pytest.raises(Exception):  # pydantic.ValidationError
            AppConfig()

    def test_port_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端口越界(>65535)应被 ge/le 约束拒绝。"""
        monkeypatch.setenv("APP_PORT", "99999")
        from app.config import AppConfig, reset_settings

        reset_settings()
        with pytest.raises(Exception):
            AppConfig()


class TestLLMConfig:
    """LLMConfig 从 ARK_ 前缀 + 全局 alias 读取。"""

    def test_llm_config_reads_ark_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ARK_API_KEY / ARK_MODEL 走 env_prefix=ARK_。"""
        monkeypatch.setenv("ARK_API_KEY", "sk-test-123")
        monkeypatch.setenv("ARK_MODEL", "deepseek-test")
        from app.config import LLMConfig, reset_settings

        reset_settings()
        cfg = LLMConfig()

        assert cfg.api_key.get_secret_value() == "sk-test-123"
        assert cfg.model == "deepseek-test"

    def test_llm_config_reads_global_aliases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_tokens / temperature / timeout 走全局 alias(无 ARK_ 前缀)。"""
        monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
        monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
        monkeypatch.setenv("LLM_TIMEOUT", "30")
        from app.config import LLMConfig, reset_settings

        reset_settings()
        cfg = LLMConfig()

        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.2
        assert cfg.timeout == 30.0

    def test_api_key_is_secret_str_not_plain_str(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """api_key 应为 SecretStr,而非普通 str,防止日志泄露。"""
        monkeypatch.setenv("ARK_API_KEY", "sk-secret")
        from app.config import LLMConfig, reset_settings
        from pydantic import SecretStr

        reset_settings()
        cfg = LLMConfig()
        assert isinstance(cfg.api_key, SecretStr)
        # repr 不含明文
        assert "sk-secret" not in repr(cfg)


class TestDatabaseConfig:
    """DatabaseConfig 优先用 DATABASE_URL,否则按字段拼接。"""

    def test_explicit_database_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """提供 DATABASE_URL 时 url 属性直接返回它。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/mydb"
        )
        from app.config import DatabaseConfig, reset_settings

        reset_settings()
        cfg = DatabaseConfig()
        assert cfg.url == "postgresql+asyncpg://u:p@db:5432/mydb"

    def test_url_built_from_fields_when_no_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 DATABASE_URL 时用 host/user/password/db 拼接,且用 asyncpg driver。"""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "dbhost")
        monkeypatch.setenv("POSTGRES_PORT", "6543")
        monkeypatch.setenv("POSTGRES_USER", "appuser")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss word")
        monkeypatch.setenv("POSTGRES_DB", "appdb")
        from app.config import DatabaseConfig, reset_settings

        reset_settings()
        cfg = DatabaseConfig()
        url = cfg.url
        assert url.startswith("postgresql+asyncpg://")
        assert "appuser" in url
        # 密码含特殊字符应被 URL 编码(@ -> %40,空格 -> +)
        assert "p%40ss+word" in url or "p%40ss%20word" in url
        assert "dbhost:6543" in url
        assert url.endswith("/appdb")

    def test_password_is_secret_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据库密码用 SecretStr 包装。"""
        monkeypatch.setenv("POSTGRES_PASSWORD", "supersecret")
        from app.config import DatabaseConfig, reset_settings
        from pydantic import SecretStr

        reset_settings()
        cfg = DatabaseConfig()
        assert isinstance(cfg.password, SecretStr)


class TestRAGConfig:
    """RAGConfig 含 overlap < chunk_size 的防死循环校验。"""

    def test_rag_defaults(self) -> None:
        """无 env 时 RAG 配置有合理默认值。"""
        from app.config import RAGConfig, reset_settings

        reset_settings()
        cfg = RAGConfig()
        assert cfg.chunk_size == 500
        assert cfg.chunk_overlap == 50
        assert cfg.top_k == 5
        assert cfg.min_similarity == 0.7

    def test_overlap_must_be_less_than_chunk_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """overlap >= chunk_size 应报错(防切块死循环)。"""
        monkeypatch.setenv("RAG_CHUNK_SIZE", "100")
        monkeypatch.setenv("RAG_CHUNK_OVERLAP", "100")
        from app.config import RAGConfig, reset_settings

        reset_settings()
        with pytest.raises(Exception):
            RAGConfig()

    def test_overlap_greater_than_size_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """overlap > chunk_size 也应报错。"""
        monkeypatch.setenv("RAG_CHUNK_SIZE", "50")
        monkeypatch.setenv("RAG_CHUNK_OVERLAP", "80")
        from app.config import RAGConfig, reset_settings

        reset_settings()
        with pytest.raises(Exception):
            RAGConfig()

    def test_valid_overlap_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """overlap < chunk_size 应正常构造。"""
        monkeypatch.setenv("RAG_CHUNK_SIZE", "500")
        monkeypatch.setenv("RAG_CHUNK_OVERLAP", "49")
        from app.config import RAGConfig, reset_settings

        reset_settings()
        cfg = RAGConfig()
        assert cfg.chunk_overlap == 49


class TestCORSConfig:
    """CORSConfig origins 解析。

    注意:origins 为 list[str](complex 类型),pydantic-settings 会先对 complex
    字段做 JSON 解码。因此 ``CORS_ORIGINS`` 应传 JSON 数组字符串
    (如 ``["http://a.com","http://b.com"]``)。config 中的 ``_split_origins``
    before-validator 主要处理已解码后的 list/tuple 输入(如代码内直传)。
    """

    def test_json_array_parsed_to_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CORS_ORIGINS 传 JSON 数组字符串应解析为列表。"""
        monkeypatch.setenv("CORS_ORIGINS", '["http://a.com","http://b.com"]')
        from app.config import CORSConfig, reset_settings

        reset_settings()
        cfg = CORSConfig()
        assert cfg.origins == ["http://a.com", "http://b.com"]

    def test_single_origin_json_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单个 origin 用 JSON 数组包裹也应解析为单元素列表。"""
        monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173"]')
        from app.config import CORSConfig, reset_settings

        reset_settings()
        cfg = CORSConfig()
        assert cfg.origins == ["http://localhost:5173"]

    def test_default_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未设置 CORS_ORIGINS 时使用默认值。"""
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        from app.config import CORSConfig, reset_settings

        reset_settings()
        cfg = CORSConfig()
        assert isinstance(cfg.origins, list)
        assert len(cfg.origins) >= 1
        assert "http://localhost:5173" in cfg.origins

    def test_split_origins_validator_handles_list_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_split_origins validator 能处理 list 输入(JSON 数组带空白)。

        字段用 alias=CORS_ORIGINS 且未开 populate_by_name,故用 env 传 JSON
        数组验证 validator 对元素的 strip 规整。
        """
        monkeypatch.setenv("CORS_ORIGINS", '["http://a.com"," http://b.com "]')
        from app.config import CORSConfig, reset_settings

        reset_settings()
        cfg = CORSConfig()
        # validator 应 strip 每个元素的两端空白
        assert cfg.origins == ["http://a.com", "http://b.com"]


class TestRateLimitConfig:
    """RateLimitConfig 提供 slowapi 可识别的限流字符串。"""

    def test_limiter_limit_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """limiter_limit 应为 '<n>/minute' 格式。"""
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "120")
        from app.config import RateLimitConfig, reset_settings

        reset_settings()
        cfg = RateLimitConfig()
        assert cfg.limiter_limit == "120/minute"


class TestSettingsAggregation:
    """Settings 聚合所有子配置,并提供生产环境校验。"""

    def test_settings_aggregates_all_groups(self) -> None:
        """Settings 应包含 app/llm/database/jwt/cors/rate_limit/rag 七组。"""
        from app.config import Settings, reset_settings

        reset_settings()
        s = Settings()
        for attr in ("app", "llm", "database", "jwt", "cors", "rate_limit", "rag"):
            assert hasattr(s, attr), f"Settings 缺少 {attr} 分组"

    def test_get_settings_is_cached_singleton(self) -> None:
        """get_settings 应返回同一实例(lru_cache)。"""
        from app.config import get_settings, reset_settings

        reset_settings()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_reset_settings_clears_cache(self) -> None:
        """reset_settings 后再取应得到新实例。"""
        from app.config import get_settings, reset_settings

        reset_settings()
        s1 = get_settings()
        reset_settings()
        s2 = get_settings()
        assert s1 is not s2

    def test_validate_production_passes_with_real_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产环境配齐真实密钥时 validate_production 不报错。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "a-real-random-64-char-secret-key-xxx-yyy-zzz")
        monkeypatch.setenv("ARK_API_KEY", "real-ark-key")
        monkeypatch.setenv("POSTGRES_PASSWORD", "real-strong-pwd")
        from app.config import Settings, reset_settings

        reset_settings()
        s = Settings()
        # 不应抛异常
        s.validate_production()

    def test_validate_production_fails_with_default_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产环境用默认/空密钥时 validate_production 应抛 ValueError。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "change_me_to_a_random_64_char_string")
        monkeypatch.setenv("ARK_API_KEY", "your_ark_api_key_here")
        monkeypatch.setenv("POSTGRES_PASSWORD", "change_me_in_production")
        from app.config import Settings, reset_settings

        reset_settings()
        s = Settings()
        with pytest.raises(ValueError, match="生产环境配置校验失败"):
            s.validate_production()

    def test_validate_production_skipped_in_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非生产环境即便密钥为默认值,validate_production 也直接跳过。"""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("JWT_SECRET_KEY", "change_me_to_a_random_64_char_string")
        from app.config import Settings, reset_settings

        reset_settings()
        s = Settings()
        s.validate_production()  # 不抛异常


class TestSecretStrSafety:
    """敏感字段(SecretStr)不打印明文。"""

    def test_jwt_secret_not_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT secret 的 repr 不应含明文。"""
        monkeypatch.setenv("JWT_SECRET_KEY", "plaintext-secret-should-not-leak")
        from app.config import JWTConfig, reset_settings

        reset_settings()
        cfg = JWTConfig()
        assert "plaintext-secret-should-not-leak" not in repr(cfg)
        assert "plaintext-secret-should-not-leak" not in str(cfg)

    def test_jwt_secret_get_secret_value_returns_plain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_secret_value() 才返回明文(受控访问点)。

        字段用 alias=JWT_SECRET_KEY 且未开 populate_by_name,故用 env 设置。
        """
        monkeypatch.setenv("JWT_SECRET_KEY", "controlled-access-value")
        from app.config import JWTConfig, reset_settings

        reset_settings()
        cfg = JWTConfig()
        assert cfg.secret_key.get_secret_value() == "controlled-access-value"

    def test_default_secret_key_emits_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认/空 JWT_SECRET_KEY 应触发警告(非生产兜底)。"""
        monkeypatch.setenv("JWT_SECRET_KEY", "change_me_to_a_random_64_char_string")
        from app.config import JWTConfig, reset_settings

        reset_settings()
        with pytest.warns(UserWarning):
            JWTConfig()
