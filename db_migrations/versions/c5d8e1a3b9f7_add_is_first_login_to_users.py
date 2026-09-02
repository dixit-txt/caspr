"""add_is_first_login_to_users

Revision ID: c5d8e1a3b9f7
Revises: b2e7f4a9c8d3
Create Date: 2026-05-04 10:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d8e1a3b9f7'
down_revision: Union[str, None] = 'b2e7f4a9c8d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_first_login column to users table.

    ``server_default='false'`` is used so:
      - All pre-existing rows get False automatically (they have already logged in).
      - The NOT NULL constraint is satisfied immediately with no separate UPDATE.
    New rows created after this migration will receive True from the SQLAlchemy
    ORM ``default=True`` set on the model column.
    """
    op.add_column(
        'users',
        sa.Column(
            'is_first_login',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
            comment=(
                "Set to True on the user's first signup. Flipped back to False "
                "automatically on the first successful login so the frontend can "
                "show the app walkthrough once."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the is_first_login column from users table."""
    op.drop_column('users', 'is_first_login')
