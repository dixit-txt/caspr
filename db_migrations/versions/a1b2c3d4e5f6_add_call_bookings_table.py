"""add_call_bookings_table

Revision ID: a1b2c3d4e5f6
Revises: e9b623b3f801
Create Date: 2026-02-02

Add call_bookings table for storing call booking requests (name, email, phone, brief).

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e9b623b3f801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "call_bookings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            comment="Name of the person booking the call",
        ),
        sa.Column(
            "email", sa.String(length=255), nullable=False, comment="Email address of the requester"
        ),
        sa.Column(
            "phone_country_code",
            sa.String(length=20),
            nullable=False,
            comment="Country code of the phone number",
        ),
        sa.Column(
            "phone_number",
            sa.String(length=50),
            nullable=False,
            comment="Phone number without country code",
        ),
        sa.Column(
            "brief", sa.Text(), nullable=True, comment="Optional brief or description from the user"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when the booking was created",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_call_bookings_email", "call_bookings", ["email"], unique=False)
    op.create_index("idx_call_bookings_created_at", "call_bookings", ["created_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_call_bookings_created_at", table_name="call_bookings")
    op.drop_index("idx_call_bookings_email", table_name="call_bookings")
    op.drop_table("call_bookings")
