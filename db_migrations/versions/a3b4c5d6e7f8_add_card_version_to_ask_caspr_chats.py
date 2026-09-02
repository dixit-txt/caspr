"""add card_version column to ask_caspr_chats table

Revision ID: a3b4c5d6e7f8
Revises: f2a8c1d3e5b7
Create Date: 2026-02-10 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a8c1d3e5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add card_version column to ask_caspr_chats table for linking to cards.version timeline."""
    # Step 1: Add column as nullable first
    op.add_column(
        'ask_caspr_chats',
        sa.Column(
            'card_version',
            sa.Integer(),
            nullable=True,
            comment='cards.version at the time this entry was created/updated — links to card timeline for version navigation'
        )
    )

    # Step 2: Backfill existing rows with default value 1
    op.execute("UPDATE ask_caspr_chats SET card_version = 1 WHERE card_version IS NULL")

    # Step 3: Set NOT NULL constraint now that all rows have a value
    op.alter_column('ask_caspr_chats', 'card_version', nullable=False)


def downgrade() -> None:
    """Remove card_version column from ask_caspr_chats table."""
    op.drop_column('ask_caspr_chats', 'card_version')
