"""add costtracker table

Revision ID: a4b5c6d7e8f9
Revises: e1f2a3b4c5d6
Create Date: 2026-07-14

Additive-only migration — creates the ``costtracker`` append-only log for
per-LLM-call token usage.

Columns (id PK + 10 payload fields = 11 columns):
  timestamp, model_name, context, agent_name, chat_id, user_id,
  usage_metadata (JSONB), input_tokens, output_tokens, created_at
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "costtracker",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the LLM call occurred (UTC)",
        ),
        sa.Column(
            "model_name",
            sa.String(length=100),
            nullable=False,
            comment="Model id, e.g. gpt-4o",
        ),
        sa.Column(
            "context",
            sa.String(length=255),
            nullable=True,
            comment="Call context label, e.g. normal_flow_fallback",
        ),
        sa.Column(
            "agent_name",
            sa.String(length=255),
            nullable=True,
            comment="Agent / stage that made the call",
        ),
        sa.Column(
            "chat_id",
            sa.String(length=255),
            nullable=True,
            comment="Chat id associated with the call",
        ),
        sa.Column(
            "user_id",
            sa.String(length=255),
            nullable=True,
            comment="User id associated with the call",
        ),
        sa.Column(
            "usage_metadata",
            JSONB,
            nullable=True,
            comment="Full provider usage blob from the LLM response",
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Prompt / input token count",
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Completion / output token count",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this costtracker row was inserted (UTC)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index("idx_costtracker_timestamp", "costtracker", ["timestamp"], unique=False)
    op.create_index("idx_costtracker_user_id", "costtracker", ["user_id"], unique=False)
    op.create_index("idx_costtracker_chat_id", "costtracker", ["chat_id"], unique=False)
    op.create_index("idx_costtracker_model_name", "costtracker", ["model_name"], unique=False)
    op.create_index("idx_costtracker_created_at", "costtracker", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_costtracker_created_at", table_name="costtracker")
    op.drop_index("idx_costtracker_model_name", table_name="costtracker")
    op.drop_index("idx_costtracker_chat_id", table_name="costtracker")
    op.drop_index("idx_costtracker_user_id", table_name="costtracker")
    op.drop_index("idx_costtracker_timestamp", table_name="costtracker")
    op.drop_table("costtracker")
