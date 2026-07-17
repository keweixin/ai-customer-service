"""应用配置管理模块。

基于 pydantic-settings 从环境变量 / .env 文件加载配置,分组管理:
AppConfig / LLMConfig / DatabaseConfig / JWTConfig / CORSConfig / RateLimitConfig / RAGConfig

通过 ``get_settings()`` 获取单例实例,内部使用 ``functools.lru_cache`` 保证
进程内只解析一次,既避免重复 IO 又保证配置一致性。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """应用级配置(运行环境、监听地址、日志级别)。"""

    model_config = SettingsConfigDict(env_prefix="APP_", extra="ignore")

    name: str = Field(default="AI Customer Service", description="应用名称")
    env: Literal["development", "staging", "production"] = Field(
        default="development", description="运行环境"
    )
    debug: bool = Field(default=False, description="是否开启调试模式")
    host: str = Field(default="0.0.0.0", description="监听地址")
    port: int = Field(default=8000, ge=1, le=65535, description="监听端口")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", description="日志级别"
    )

    @property
    def is_production(self) -> bool:
        """是否生产环境(决定日志渲染方式、错误详情是否外泄等)。"""
        return self.env == "production"


class LLMConfig(BaseSettings):
    """火山方舟 LLM 配置。

    所有 LLM 调用都通过方舟 OpenAI 兼容接口,这里集中管理接入点、密钥、超时与重试。
    """

    model_config = SettingsConfigDict(env_prefix="ARK_", extra="ignore")

    api_key: SecretStr = Field(
        default=SecretStr(""), description="方舟 API Key,敏感信息"
    )
    model: str = Field(default="deepseek-v4-flash", description="默认对话模型")
    base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/coding/v3",
        description="方舟 API 基础 URL",
    )

    # 下面的配置项不带 ARK_ 前缀,需单独从全局命名空间读取,
    # 因此使用 nested BaseSettings 时通过 alias 显式映射。
    max_tokens: int = Field(
        default=2048, ge=1, le=32768, alias="LLM_MAX_TOKENS", description="单次生成最大 token"
    )
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, alias="LLM_TEMPERATURE", description="采样温度"
    )
    timeout: float = Field(
        default=60.0, gt=0, alias="LLM_TIMEOUT", description="单次调用超时(秒)"
    )
    max_retries: int = Field(
        default=3, ge=0, le=10, alias="LLM_MAX_RETRIES", description="失败重试次数"
    )

    # Embedding 配置(也走方舟)
    embedding_model: str = Field(
        default="", alias="ARK_EMBEDDING_MODEL", description="Embedding 接入点 ID"
    )
    embedding_dimension: int = Field(
        default=1024, ge=1, alias="EMBEDDING_DIMENSION", description="向量维度"
    )


class DatabaseConfig(BaseSettings):
    """PostgreSQL 数据库配置。

    优先使用显式拼接的 ``DATABASE_URL``;若未提供则由各字段自动拼接,
    方便本地开发只填 host/user/password 等即可。
    """

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host: str = Field(default="localhost")
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = Field(default="aics")
    password: SecretStr = Field(default=SecretStr(""), description="数据库密码,敏感信息")
    db: str = Field(default="ai_customer_service", alias="POSTGRES_DB")
    database_url: str | None = Field(
        default=None, alias="DATABASE_URL", description="完整连接串,优先使用"
    )

    @property
    def url(self) -> str:
        """返回最终用于 SQLAlchemy 的异步连接串。

        优先使用显式 DATABASE_URL;否则用单字段拼装,确保 asyncpg driver。
        """
        if self.database_url:
            return self.database_url
        # 拼接时对密码做 URL 安全编码,避免特殊字符(如 @、#)破坏连接串
        from urllib.parse import quote_plus

        pwd = quote_plus(self.password.get_secret_value())
        return (
            f"postgresql+asyncpg://{self.user}:{pwd}@{self.host}:{self.port}/{self.db}"
        )


class JWTConfig(BaseSettings):
    """JWT 鉴权配置。"""

    model_config = SettingsConfigDict(env_prefix="JWT_", extra="ignore")

    secret_key: SecretStr = Field(
        default=SecretStr(""),
        alias="JWT_SECRET_KEY",
        description="JWT 签名密钥,生产环境必须为随机长串",
    )
    algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=1440, ge=1, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    @field_validator("secret_key")
    @classmethod
    def _validate_secret(cls, v: SecretStr) -> SecretStr:
        """生产环境强制要求非默认密钥,防止密钥泄露导致 token 伪造。"""
        value = v.get_secret_value()
        if value in {"", "change_me_to_a_random_64_char_string"}:
            # 这里只做警告式兜底,真正拒绝在 Settings 聚合层处理,避免单测环境卡死
            import warnings

            warnings.warn(
                "JWT_SECRET_KEY 使用了默认/空值,生产环境请设置为随机长串",
                stacklevel=2,
            )
        return v


class CORSConfig(BaseSettings):
    """CORS 跨域配置。

    前端域名白名单以逗号分隔字符串存储,这里解析为列表便于中间件直接消费。
    """

    model_config = SettingsConfigDict(extra="ignore")

    origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"],
        alias="CORS_ORIGINS",
        description="允许的前端来源,逗号分隔",
    )

    @field_validator("origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> list[str]:
        """支持 .env 中逗号分隔字符串,自动拆分为列表。"""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        if isinstance(v, (list, tuple)):
            return [str(o).strip() for o in v if str(o).strip()]
        raise TypeError(f"CORS_ORIGINS 无法解析: {v!r}")


class RateLimitConfig(BaseSettings):
    """限流配置(配合 slowapi)。"""

    model_config = SettingsConfigDict(extra="ignore")

    per_minute: int = Field(
        default=60, ge=1, alias="RATE_LIMIT_PER_MINUTE", description="每分钟最大请求数"
    )

    @property
    def limiter_limit(self) -> str:
        """返回 slowapi 限流装饰器可识别的字符串,如 ``60/minute``。"""
        return f"{self.per_minute}/minute"


class RAGConfig(BaseSettings):
    """RAG 检索增强生成配置。"""

    model_config = SettingsConfigDict(env_prefix="RAG_", extra="ignore")

    chunk_size: int = Field(default=500, ge=1, description="文档切块字符数")
    chunk_overlap: int = Field(default=50, ge=0, description="块之间重叠字符数")
    top_k: int = Field(default=5, ge=1, description="检索返回 top-k 片段")
    min_similarity: float = Field(
        default=0.7, ge=0.0, le=1.0, description="最低相似度阈值"
    )

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v: int, info) -> int:
        """overlap 必须小于 chunk_size,否则切块会陷入死循环。"""
        size = info.data.get("chunk_size", 500)
        if v >= size:
            raise ValueError(
                f"RAG_CHUNK_OVERLAP({v}) 必须小于 RAG_CHUNK_SIZE({size})"
            )
        return v


class Settings(BaseSettings):
    """聚合所有配置分组的根配置。

    通过 ``model_config`` 指定 ``.env`` 文件路径与大小写不敏感,
    子配置使用 nested 模式但各自从全局环境变量读取(借助 alias)。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    jwt: JWTConfig = Field(default_factory=JWTConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)

    def validate_production(self) -> None:
        """生产环境硬性校验:关键密钥不得为默认/空值。

        在应用启动事件中调用,提前暴露配置问题而非运行时才报错。
        """
        if not self.app.is_production:
            return

        errors: list[str] = []
        if self.jwt.secret_key.get_secret_value() in {
            "",
            "change_me_to_a_random_64_char_string",
        }:
            errors.append("生产环境 JWT_SECRET_KEY 必须为随机长串")
        if self.llm.api_key.get_secret_value() in {"", "your_ark_api_key_here"}:
            errors.append("生产环境 ARK_API_KEY 必须填写真实值")
        if self.database.password.get_secret_value() in {
            "",
            "change_me_in_production",
        }:
            errors.append("生产环境 POSTGRES_PASSWORD 必须修改默认值")

        if errors:
            raise ValueError(
                "生产环境配置校验失败:\n" + "\n".join(f"  - {e}" for e in errors)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局 Settings 单例。

    使用 ``lru_cache`` 保证进程内只解析一次环境变量/`.env` 文件,
    既减少 IO 又保证整个应用使用同一份配置。如需热更新,调用
    ``get_settings.cache_clear()``。
    """
    return Settings()


def reset_settings() -> None:
    """清除缓存的配置单例,主要用于测试场景需要切换 .env 时调用。"""
    get_settings.cache_clear()
