"""change_subscription_id_to_varchar

Revision ID: fe8afc0b5a5f
Revises: 3b249acb3ed1
Create Date: 2026-02-01 14:44:54.851823

Change subscription_id column from CHAR(100) to VARCHAR(100) to eliminate
trailing space padding issues when integrating with Razorpay API.

Background:
- CHAR(100) is fixed-length and pads values with spaces
- VARCHAR(100) is variable-length with no padding
- The spaces caused 404 errors when calling Razorpay API
- This migration eliminates the need for .strip() calls everywhere

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "fe8afc0b5a5f"
down_revision: str | None = "3b249acb3ed1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Upgrade schema: Change subscription_id from CHAR(100) to VARCHAR(100).

    Steps:
    1. Trim existing subscription_id values to remove trailing spaces
    2. Change column type from CHAR to VARCHAR
    """
    # Step 1: Data migration - Trim all existing subscription_id values
    # This removes the trailing spaces from CHAR padding
    op.execute("""
        UPDATE subscriptions
        SET subscription_id = TRIM(subscription_id)
        WHERE subscription_id IS NOT NULL
    """)

    # Step 2: Schema migration - Change column type from CHAR(100) to VARCHAR(100)
    # PostgreSQL allows this with a simple ALTER COLUMN
    op.alter_column(
        "subscriptions",
        "subscription_id",
        existing_type=sa.CHAR(100),
        type_=sa.String(100),
        existing_nullable=True,
        existing_comment="Reference to external subscription id (Razorpay subscription ID)",
    )

    print("✅ Migration complete: subscription_id changed from CHAR(100) to VARCHAR(100)")
    print("✅ All existing subscription_id values trimmed to remove spaces")


def downgrade() -> None:
    """
    Downgrade schema: Change subscription_id from VARCHAR(100) back to CHAR(100).

    Warning: This will re-add space padding to values!
    """
    # Change column type from VARCHAR(100) back to CHAR(100)
    op.alter_column(
        "subscriptions",
        "subscription_id",
        existing_type=sa.String(100),
        type_=sa.CHAR(100),
        existing_nullable=True,
        existing_comment="Reference to external subscription id",
    )

    print("⚠️  Rollback complete: subscription_id changed back to CHAR(100)")
    print("⚠️  Note: Values will be space-padded again!")
