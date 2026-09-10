"""add_invalidated_status_to_transaction_status_enum

Revision ID: d6541feb12d8
Revises: a1b2c3d4e5f6
Create Date: 2026-02-05 13:56:29.391234

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d6541feb12d8"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add 'INVALIDATED' value to transaction_status_enum.

    This new status is used to mark token batches that were invalidated
    during subscription plan changes. Unlike REVERSED (which implies a refund),
    INVALIDATED indicates the batch is replaced by new batches with tokens
    carried forward.
    """
    # Add new enum value to the existing transaction_status_enum type
    op.execute("ALTER TYPE transaction_status_enum ADD VALUE IF NOT EXISTS 'INVALIDATED'")


def downgrade() -> None:
    """
    Remove 'invalidated' value from transaction_status_enum.

    Note: PostgreSQL does not support removing enum values directly.
    If downgrade is needed, you would need to:
    1. Create a new enum without 'invalidated'
    2. Alter all columns using the old enum to use the new enum
    3. Drop the old enum
    4. Rename the new enum

    This is complex and risky, so we're leaving it as a no-op.
    Ensure no data uses 'invalidated' status before attempting downgrade.
    """
    # PostgreSQL doesn't support removing enum values
    # Downgrade would require recreating the enum, which is risky
    # Leave as no-op - manual intervention required if needed
    pass
