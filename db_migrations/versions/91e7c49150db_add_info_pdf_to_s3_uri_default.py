"""add_info_pdf_to_s3_uri_default

Revision ID: 91e7c49150db
Revises: 308c0341df21
Create Date: 2026-01-06 22:45:35.870876

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '91e7c49150db'
down_revision: Union[str, None] = '308c0341df21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add info_pdf field to s3_uri JSONB default value.
    
    Note: This only affects NEW rows. Existing rows will get the info_pdf field
    added automatically by the application logic in async_db_functions.py when
    s3_uri is updated (see the ensure keys logic).
    """
    # Update server default for reports table
    op.alter_column(
        'reports',
        's3_uri',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default='{"md": null, "pdf": null, "html": null, "pptx": null, "info_pdf": null}',
        existing_nullable=False,
        existing_comment="S3 URIs of current version (denormalized for quick access)"
    )


def downgrade() -> None:
    """
    Remove info_pdf field from s3_uri JSONB default value.
    """
    # Revert server default for reports table
    op.alter_column(
        'reports',
        's3_uri',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default='{"md": null, "pdf": null, "html": null, "pptx": null}',
        existing_nullable=False,
        existing_comment="S3 URIs of current version (denormalized for quick access)"
    )
