"""add meta_data column in tokenbatch table

Revision ID: 04e46567fad2
Revises: 567be0211968
Create Date: 2026-01-26 00:36:35.363734

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "04e46567fad2"
down_revision: str | None = "567be0211968"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.add_column(
        "token_batches",
        sa.Column(
            "meta_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Additional data (error messages, token_batch_ids for multi-batch operations, etc.)",
        ),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_column("token_batches", "meta_data")

    # ### end Alembic commands ###
