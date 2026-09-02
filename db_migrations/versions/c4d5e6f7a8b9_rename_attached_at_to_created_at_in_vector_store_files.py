"""rename attached_at to created_at in vector_store_files

Revision ID: c4d5e6f7a8b9
Revises: b1c2d3e4f5a6
Create Date: 2026-02-25 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename 'attached_at' column to 'created_at' in vector_store_files table."""
    op.alter_column(
        'vector_store_files',
        'attached_at',
        new_column_name='created_at',
        comment='When file was attached to vector store',
    )


def downgrade() -> None:
    """Revert 'created_at' column back to 'attached_at' in vector_store_files table."""
    op.alter_column(
        'vector_store_files',
        'created_at',
        new_column_name='attached_at',
        comment='When file was attached to vector store',
    )

