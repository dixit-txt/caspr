"""add subscription status enum values

Revision ID: f9c2d7e3a4b1
Revises: 8f81976940e6
Create Date: 2026-01-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9c2d7e3a4b1'
down_revision: Union[str, None] = '8f81976940e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add new enum values to subscription_status_enum.
    
    Adding:
    - CREATED: Subscription created, awaiting user approval
    - AUTHENTICATED: User approved, awaiting first charge
    - CHARGED: Payment successful (recurring state)
    - HALTED: Payment failed permanently
    - COMPLETED: All billing cycles completed
    
    Note: PENDING already exists from original migration
    """
    # PostgreSQL requires ALTER TYPE to add enum values
    # IF NOT EXISTS is supported in PostgreSQL 9.1+
    
    op.execute("ALTER TYPE subscription_status_enum ADD VALUE IF NOT EXISTS 'CREATED'")
    op.execute("ALTER TYPE subscription_status_enum ADD VALUE IF NOT EXISTS 'AUTHENTICATED'")
    op.execute("ALTER TYPE subscription_status_enum ADD VALUE IF NOT EXISTS 'CHARGED'")
    op.execute("ALTER TYPE subscription_status_enum ADD VALUE IF NOT EXISTS 'HALTED'")
    op.execute("ALTER TYPE subscription_status_enum ADD VALUE IF NOT EXISTS 'COMPLETED'")


def downgrade() -> None:
    """
    Downgrade not supported - PostgreSQL doesn't support removing enum values directly.
    
    To remove enum values, you would need to:
    1. Create a new enum type with the old values
    2. Alter all columns using the enum to use the new type
    3. Drop the old enum type
    4. Rename the new enum type
    
    This is complex and risky, so downgrade is not implemented.
    If needed, perform manual database operations.
    """
    pass

