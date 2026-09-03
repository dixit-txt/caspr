"""drop caspr MCP api keys table and messages.is_mcp

Removes the schema left behind by the Caspr MCP server feature:
  - ``mcp_api_keys`` (added in a9b0c1d2e3f4)
  - ``messages.is_mcp`` (added in c2d3e4f5a6b7)

Revision ID: d2e3f4a5b6c7
Revises: 5bd11a6275f4
Create Date: 2026-09-02

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "5bd11a6275f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the MCP API key table and the MCP-origin flag on messages."""
    op.execute(sa.text("DROP INDEX IF EXISTS ix_mcp_api_keys_key_hash"))
    op.execute(sa.text("DROP TABLE IF EXISTS mcp_api_keys"))
    op.execute(sa.text("ALTER TABLE messages DROP COLUMN IF EXISTS is_mcp"))


def downgrade() -> None:
    """Recreate the MCP API key table and messages.is_mcp (keys are not recoverable)."""
    op.add_column(
        "messages",
        sa.Column(
            "is_mcp",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Whether this chat was originated via an MCP API key",
        ),
    )
    op.create_table(
        "mcp_api_keys",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.CHAR(length=36),
            nullable=False,
            comment="Owning user (exactly one MCP API key per user)",
        ),
        sa.Column(
            "key_prefix",
            sa.String(length=24),
            nullable=False,
            comment="Non-secret prefix for UI display",
        ),
        sa.Column(
            "key_hash",
            sa.String(length=64),
            nullable=False,
            comment="HMAC-SHA256 hex digest of the key",
        ),
        sa.Column(
            "last_four",
            sa.String(length=4),
            nullable=False,
            comment="Last 4 characters of the plaintext key",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful auth with this key",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_mcp_api_keys_user_id"),
        sa.UniqueConstraint("key_hash", name="uq_mcp_api_keys_key_hash"),
    )
    op.create_index("ix_mcp_api_keys_key_hash", "mcp_api_keys", ["key_hash"], unique=False)
