"""remove_unique_constraint_from_subscriptions_user_id

Revision ID: e9b623b3f801
Revises: f19f5978fb69
Create Date: 2026-02-02 15:12:34.604366

Remove unique constraint from subscriptions.user_id to allow multiple
subscription records per user (for history tracking).

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e9b623b3f801"
down_revision: str | None = "f19f5978fb69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove unique constraint from subscriptions.user_id."""
    # Drop the unique constraint on user_id
    op.drop_constraint("subscriptions_user_id_key", "subscriptions", type_="unique")

    print("✅ Removed unique constraint from subscriptions.user_id")
    print("   Users can now have multiple subscription records (for history)")


def downgrade() -> None:
    """Re-add unique constraint to subscriptions.user_id."""
    # Add back the unique constraint on user_id
    op.create_unique_constraint("subscriptions_user_id_key", "subscriptions", ["user_id"])

    print("⚠️  Re-added unique constraint to subscriptions.user_id")
    print("   Note: This may fail if there are duplicate user_id values")
