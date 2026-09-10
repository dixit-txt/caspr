"""add_bonus_to_transaction_source_enum

Revision ID: a7f3e9c2d1b8
Revises: a3b4c5d6e7f8
Create Date: 2026-02-13 11:40:00.000000

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a7f3e9c2d1b8"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add 'BONUS' value to transaction_source_enum.

    This new source type is used for manual bonus credits added by administrators
    through the manual_credit_tokens.py script. It distinguishes admin-granted
    bonuses from automatic signup bonuses.
    """
    # Add new enum value to the existing transaction_source_enum type
    op.execute("ALTER TYPE transaction_source_enum ADD VALUE IF NOT EXISTS 'BONUS'")


def downgrade() -> None:
    """
    Remove 'BONUS' value from transaction_source_enum.

    Note: PostgreSQL does not support removing enum values directly.
    If downgrade is needed, you would need to:
    1. Create a new enum without 'BONUS'
    2. Alter all columns using the old enum to use the new enum
    3. Drop the old enum
    4. Rename the new enum

    This is complex and risky, so we're leaving it as a no-op.
    Ensure no data uses 'BONUS' source type before attempting downgrade.
    """
    # PostgreSQL doesn't support removing enum values
    # Downgrade would require recreating the enum, which is risky
    # Leave as no-op - manual intervention required if needed
    pass
