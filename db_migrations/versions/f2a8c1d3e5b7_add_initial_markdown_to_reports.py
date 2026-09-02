"""add initial_markdown column to reports table

Revision ID: f2a8c1d3e5b7
Revises: ead0237747ee
Create Date: 2026-02-10 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f2a8c1d3e5b7'
down_revision: Union[str, None] = 'ead0237747ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add initial_markdown column to reports table for Ask Caspr S3 file path."""
    op.add_column(
        'reports',
        sa.Column(
            'initial_markdown',
            sa.String(length=500),
            nullable=True,
            comment='S3 path of the initial markdown file for Ask Caspr'
        )
    )


def downgrade() -> None:
    """Remove initial_markdown column from reports table."""
    op.drop_column('reports', 'initial_markdown')

