"""add_poster_image_url_to_report_versions

Revision ID: 0e37808ea016
Revises: c3f7a8d9e1b2
Create Date: 2025-12-20 17:08:46.012494

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0e37808ea016'
down_revision: Union[str, None] = 'c3f7a8d9e1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add poster_image_url column to report_versions table.
    Migrate existing poster_image_url from reports table to report_versions table for version 1.
    """
    # Add poster_image_url column to report_versions table
    op.add_column(
        'report_versions',
        sa.Column('poster_image_url', sa.String(length=500), nullable=True, comment="S3 URL for the poster/thumbnail image")
    )
    
    # Migrate existing poster_image_url from reports table to report_versions table
    # Only for version 1 (first version of each report)
    op.execute("""
        UPDATE report_versions rv
        SET poster_image_url = r.poster_image_url
        FROM reports r
        WHERE rv.report_id = r.id
          AND r.poster_image_url IS NOT NULL
    """)


def downgrade() -> None:
    """Remove poster_image_url column from report_versions table."""
    op.drop_column('report_versions', 'poster_image_url')
