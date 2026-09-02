"""update_card_versions_table_add_section_and_subsection_tracking

Revision ID: 3043ccb251e7
Revises: d4e8f5a1c3b4
Create Date: 2026-01-05 15:56:03.831207

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3043ccb251e7'
down_revision: Union[str, None] = 'd4e8f5a1c3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add new section_id column (stores cards.card_id - business identifier)
    op.add_column(
        'card_versions',
        sa.Column(
            'section_id',
            sa.CHAR(36),
            nullable=True,  # Nullable initially for existing data
            comment="Business card_id from cards.card_id - for tracking which logical card was modified"
        )
    )
    
    # Add new subsection_id column (nullable since section-level edits won't have this)
    op.add_column(
        'card_versions',
        sa.Column(
            'subsection_id',
            sa.CHAR(36),
            nullable=True,
            comment="ID of the subsection that was modified (null for section-level changes)"
        )
    )
    
    # Create index on section_id for better query performance
    op.create_index('idx_card_versions_section_id', 'card_versions', ['section_id'])
    
    # Create index on subsection_id for better query performance
    op.create_index('idx_card_versions_subsection_id', 'card_versions', ['subsection_id'])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the new indexes
    op.drop_index('idx_card_versions_subsection_id', table_name='card_versions')
    op.drop_index('idx_card_versions_section_id', table_name='card_versions')
    
    # Remove the new columns
    op.drop_column('card_versions', 'subsection_id')
    op.drop_column('card_versions', 'section_id')
