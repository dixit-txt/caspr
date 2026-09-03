"""add_t_c_verified_to_users

Revision ID: 308c0341df21
Revises: 3043ccb251e7
Create Date: 2026-01-05 17:30:32.561975

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "308c0341df21"
down_revision: str | None = "3043ccb251e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add t_c_verified column to users table with default value False
    op.add_column(
        "users",
        sa.Column(
            "t_c_verified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Whether the user has accepted Terms and Conditions",
        ),
    )

    # Create index on t_c_verified for better query performance
    op.create_index("idx_users_t_c_verified", "users", ["t_c_verified"])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the index
    op.drop_index("idx_users_t_c_verified", table_name="users")

    # Remove the t_c_verified column
    op.drop_column("users", "t_c_verified")
