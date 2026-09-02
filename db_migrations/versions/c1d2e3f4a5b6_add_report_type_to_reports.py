"""Add report_type column to reports table.

Revision ID: c1d2e3f4a5b6
Revises: b3c4d5e6f7a8
Create Date: 2026-06-17

Additive-only migration — adds a single nullable VARCHAR(10) column
to track whether a report was generated as 'study' or 'brief'.
Existing rows default to NULL (treated as 'study' by application code).
"""

from alembic import op
import sqlalchemy as sa

revision = 'c1d2e3f4a5b6'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'reports',
        sa.Column(
            'report_type',
            sa.String(10),
            nullable=True,
            comment="Report generation type: study or brief",
        )
    )


def downgrade():
    op.drop_column('reports', 'report_type')
