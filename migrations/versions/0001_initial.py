"""initial schema.

创建 AI 客服系统全部表结构、枚举、索引、pgvector 扩展与 HNSW 向量索引,
以及维护 updated_at 的触发器。

Revision ID: 0001
Revises:
Create Date: 2026-07-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# Alembic 版本标识
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级:建表 + 扩展 + 索引 + 触发器。"""

    # 1) pgvector 扩展:必须最先,后续 vector 类型依赖它。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2) gen_random_uuid() 依赖 pgcrypto(PG13+ 内置,低版本需显式启用)。
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')

    # ---------- 枚举类型 ----------
    # 显式建枚举类型而非依赖 SA Enum 自动建,便于跨迁移引用与显式控制名称。
    user_role = postgresql.ENUM("user", "admin", name="user_role", create_type=False)
    session_status = postgresql.ENUM(
        "active", "closed", "transferred", name="session_status", create_type=False
    )
    message_role = postgresql.ENUM(
        "system", "user", "assistant", "tool", name="message_role", create_type=False
    )
    source_type = postgresql.ENUM(
        "file", "url", "text", name="source_type", create_type=False
    )
    audit_action = postgresql.ENUM(
        "login", "logout", "upload_doc", "transfer_human", "call_tool",
        name="audit_action", create_type=False,
    )
    for enum in (user_role, session_status, message_role, source_type, audit_action):
        enum.create(op.get_bind(), checkfirst=True)

    # ---------- users ----------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("role", user_role, nullable=False,
                  server_default=sa.text("'user'")),
        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        comment="用户表",
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_email", "users", ["email"])

    # ---------- user_profiles(一对一) ----------
    op.create_table(
        "user_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_data", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 一对一:user_id 唯一;ondelete CASCADE 删用户连带删画像。
        sa.UniqueConstraint("user_id", name="uq_user_profiles_user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        comment="用户画像(长期记忆)",
    )
    op.create_index("ix_user_profiles_user_id", "user_profiles", ["user_id"])

    # ---------- sessions ----------
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", session_status, nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        comment="会话表",
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])

    # ---------- messages ----------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.String, nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        # 列名 metadata 与 SQLAlchemy 保留属性冲突,ORM 用 metadata_ 映射到此列。
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        comment="消息表",
    )
    # 复合索引:对话回放核心查询(按会话+时间正序)。
    op.create_index(
        "ix_messages_session_created", "messages", ["session_id", "created_at"]
    )

    # ---------- knowledge_docs ----------
    op.create_table(
        "knowledge_docs",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source_type", source_type, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("chunks_count", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        comment="知识库文档",
    )

    # ---------- knowledge_chunks(向量存储) ----------
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        # 向量列:Vector(1024) 与 EMBEDDING_DIMENSION 对齐;nullable 容忍异步回填。
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["doc_id"], ["knowledge_docs.id"], ondelete="CASCADE"),
        comment="知识库文档块(向量存储)",
    )
    op.create_index(
        "ix_knowledge_chunks_doc_index",
        "knowledge_chunks",
        ["doc_id", "chunk_index"],
    )
    # HNSW 向量索引:近似最近邻,余弦距离。
    # 参数 m=16/ef_construction=64 为 pgvector 推荐默认,平衡召回率与建索引速度;
    # vector_cosine_ops 必须与检索算子 <=> 匹配,否则索引不命中。
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # ---------- audit_logs ----------
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        # ondelete SET NULL:删用户后审计记录保留(user_id 置空)。
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("target", sa.String(512), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        comment="审计日志(只追加)",
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # ---------- updated_at 触发器 ----------
    # 为所有带 updated_at 的表建触发器:UPDATE 时自动刷新 updated_at,
    # 保证即便绕过 ORM(裸 SQL/其他客户端)也能正确维护时间戳。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("users", "user_profiles", "sessions", "knowledge_docs"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION refresh_updated_at();
            """
        )


def downgrade() -> None:
    """降级:按建表逆序删表 + 枚举 + 扩展。"""
    # 先删触发器与函数
    for table in ("users", "user_profiles", "sessions", "knowledge_docs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS refresh_updated_at()")

    op.drop_table("audit_logs")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_docs")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("user_profiles")
    op.drop_table("users")

    # 删枚举类型
    for enum_name in (
        "audit_action", "source_type", "message_role", "session_status", "user_role"
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    # 注意:vector / pgcrypto 扩展可能被其他对象依赖,降级不删扩展,
    # 避免误伤。如确需清理,请手动 DROP EXTENSION。
