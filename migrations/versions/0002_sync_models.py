"""sync models with db schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18

修复模型与初始迁移的 schema 漂移:
- sessions 表新增 summary 列(业务服务层 agent 加的字段)
- audit_logs 表新增 message 列
- audit_logs.action 从 enum audit_action 改为 VARCHAR(128)
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # sessions 加 summary 列(对话摘要,用于超长对话压缩)
    op.add_column("sessions", sa.Column("summary", sa.Text(), nullable=True))

    # audit_logs 加 message 列(可读的操作描述)
    op.add_column(
        "audit_logs", sa.Column("message", sa.String(500), nullable=True)
    )

    # audit_logs.action 从 enum 改 VARCHAR(128)
    # 业务层用点分动作串(如 user.register/llm.chat),enum 无法承载
    op.execute(
        "ALTER TABLE audit_logs ALTER COLUMN action TYPE VARCHAR(128) "
        "USING action::text"
    )
    op.execute("DROP TYPE IF EXISTS audit_action")


def downgrade() -> None:
    # 回滚:重建 enum 并转换
    op.execute(
        "CREATE TYPE audit_action AS ENUM "
        "('login','logout','upload_doc','transfer_human','call_tool')"
    )
    op.execute(
        "ALTER TABLE audit_logs ALTER COLUMN action TYPE audit_action "
        "USING action::audit_action"
    )
    op.drop_column("audit_logs", "message")
    op.drop_column("sessions", "summary")
