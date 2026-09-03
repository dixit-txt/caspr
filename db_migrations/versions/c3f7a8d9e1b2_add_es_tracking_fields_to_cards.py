"""Add ES tracking fields to cards table

Revision ID: c3f7a8d9e1b2
Revises: b9872c6eaf60
Create Date: 2025-01-11 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3f7a8d9e1b2"
down_revision: str | None = "b9872c6eaf60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add changed_since_es column
    op.add_column(
        "cards",
        sa.Column(
            "changed_since_es",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether card changed since last ES generation",
        ),
    )

    # Add last_es_version_used column with default value of 1 for backward compatibility
    # Existing section cards are assumed to have been used in ES version 1
    op.add_column(
        "cards",
        sa.Column(
            "last_es_version_used",
            sa.Integer(),
            nullable=True,
            server_default="1",
            comment="ES version number this card summary was used in",
        ),
    )

    # Remove the server_default after it's been applied to existing rows
    # This ensures our Python code has full control over the value for new inserts
    op.alter_column("cards", "last_es_version_used", server_default=None)

    # Create index for faster queries on changed_since_es
    op.create_index("idx_cards_changed_since_es", "cards", ["changed_since_es"])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop index
    op.drop_index("idx_cards_changed_since_es", table_name="cards")

    # Drop columns
    op.drop_column("cards", "last_es_version_used")
    op.drop_column("cards", "changed_since_es")
