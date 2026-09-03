"""add_domain_id_to_reports

Revision ID: 8e9a64e15747
Revises: f1a2b3c4d5e6
Create Date: 2026-04-13 13:18:33.457541

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8e9a64e15747"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: Add domain_id column to reports table and create index."""
    op.add_column(
        "reports",
        sa.Column(
            "domain_id",
            sa.String(50),
            nullable=True,
            comment="Report domain: default, primary_research, due_diligence, industry_benchmarking, market_insight, rfp, business_plan",
        ),
    )

    op.create_index("idx_reports_domain_id", "reports", ["domain_id"])


def downgrade() -> None:
    """Downgrade schema: Remove domain_id column and index from reports table."""
    op.drop_index("idx_reports_domain_id", "reports")
    op.drop_column("reports", "domain_id")
