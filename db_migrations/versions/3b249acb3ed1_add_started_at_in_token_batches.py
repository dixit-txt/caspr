"""add started_at in token batches

Revision ID: 3b249acb3ed1
Revises: f9c2d7e3a4b1
Create Date: 2026-01-31 12:00:26.197622

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3b249acb3ed1"
down_revision: str | None = "f9c2d7e3a4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add start_at column to token_batches table."""
    # Add start_at column to track when token batch becomes available/starts
    op.add_column(
        "token_batches",
        sa.Column(
            "start_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When this batch starts/becomes available",
        ),
    )


def downgrade() -> None:
    """Remove start_at column from token_batches table."""
    # Remove start_at column
    op.drop_column("token_batches", "start_at")
