"""add dashboard_role to users for admin dashboard access

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-16

Additive — CEO/cofounder dashboard allowlist via users.dashboard_role.
Allowed values: ceo | cofounder | admin (NULL = no access).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'dashboard_role',
            sa.String(length=50),
            nullable=True,
            comment='Admin dashboard role: ceo | cofounder | admin. NULL = no dashboard access.',
        ),
    )
    op.create_index('idx_users_dashboard_role', 'users', ['dashboard_role'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_users_dashboard_role', table_name='users')
    op.drop_column('users', 'dashboard_role')
