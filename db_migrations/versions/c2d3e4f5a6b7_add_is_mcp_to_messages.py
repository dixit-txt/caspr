"""add is_mcp column to messages table

Revision ID: c2d3e4f5a6b7
Revises: a9b0c1d2e3f4
Create Date: 2026-07-30 16:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add is_mcp boolean column to messages table to distinguish MCP-originated chats."""
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


def downgrade() -> None:
    """Remove is_mcp column from messages table."""
    op.drop_column("messages", "is_mcp")
