"""rename_domain_id_to_domain_name

Revision ID: b2e7f4a9c8d3
Revises: 8e9a64e15747
Create Date: 2026-04-16 06:00:00.000000

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2e7f4a9c8d3"
down_revision: str | None = "8e9a64e15747"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename domain_id column to domain_name in reports table."""
    op.drop_index("idx_reports_domain_id", "reports")
    op.alter_column("reports", "domain_id", new_column_name="domain_name")
    op.create_index("idx_reports_domain_name", "reports", ["domain_name"])


def downgrade() -> None:
    """Revert domain_name back to domain_id."""
    op.drop_index("idx_reports_domain_name", "reports")
    op.alter_column("reports", "domain_name", new_column_name="domain_id")
    op.create_index("idx_reports_domain_id", "reports", ["domain_id"])
