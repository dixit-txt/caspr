"""add_referral_system

Revision ID: f19f5978fb69
Revises: fe8afc0b5a5f
Create Date: 2026-02-01 23:53:23.724984

Add referral system:
- Add referral_code column to users table
- Generate unique codes for existing users
- Create referrals table to track referral relationships
- Both referrer and referee get 25,000 bonus tokens

"""
from typing import Sequence, Union
import string
import secrets

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f19f5978fb69'
down_revision: Union[str, None] = 'fe8afc0b5a5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def generate_referral_code() -> str:
    """Generate a unique 8-character referral code."""
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


def upgrade() -> None:
    """Upgrade schema - Add referral system."""
    
    # Step 1: Add referral_code column to users table (nullable first)
    op.add_column('users', sa.Column('referral_code', sa.String(length=12), nullable=True))
    
    # Step 2: Generate unique referral codes for existing users
    connection = op.get_bind()
    
    # Get all existing user IDs
    result = connection.execute(text("SELECT id FROM users"))
    user_ids = [row[0] for row in result]
    
    # Generate and assign unique codes
    used_codes = set()
    for user_id in user_ids:
        # Generate unique code
        code = generate_referral_code()
        while code in used_codes:
            code = generate_referral_code()
        used_codes.add(code)
        
        # Update user with referral code
        connection.execute(
            text("UPDATE users SET referral_code = :code WHERE id = :user_id"),
            {"code": code, "user_id": user_id}
        )
    
    # Step 3: Make referral_code NOT NULL and add unique constraint
    op.alter_column('users', 'referral_code', nullable=False)
    op.create_unique_constraint('uq_users_referral_code', 'users', ['referral_code'])
    op.create_index('ix_users_referral_code', 'users', ['referral_code'])
    
    # Step 4: Create referrals table
    op.create_table(
        'referrals',
        sa.Column('id', sa.CHAR(length=36), nullable=False),
        sa.Column('referrer_id', sa.CHAR(length=36), nullable=False),
        sa.Column('referee_id', sa.CHAR(length=36), nullable=False),
        sa.Column('referral_code_used', sa.String(length=12), nullable=False),
        sa.Column('tokens_credited_to_referrer', sa.Integer(), nullable=False, server_default='25000'),
        sa.Column('tokens_credited_to_referee', sa.Integer(), nullable=False, server_default='25000'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='completed'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['referrer_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['referee_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('referee_id', name='uq_referee_one_referral'),
        sa.CheckConstraint('referrer_id != referee_id', name='ck_no_self_referral')
    )
    
    # Step 5: Create indexes for referrals table
    op.create_index('ix_referrals_referrer_id', 'referrals', ['referrer_id'])
    op.create_index('ix_referrals_referee_id', 'referrals', ['referee_id'])
    op.create_index('ix_referrals_code', 'referrals', ['referral_code_used'])
    
    print("✅ Referral system migration complete:")
    print(f"   - Added referral_code column to users table")
    print(f"   - Generated {len(user_ids)} unique referral codes for existing users")
    print(f"   - Created referrals table with constraints")
    print(f"   - Created necessary indexes")


def downgrade() -> None:
    """Downgrade schema - Remove referral system."""
    
    # Drop referrals table and its indexes
    op.drop_index('ix_referrals_code', table_name='referrals')
    op.drop_index('ix_referrals_referee_id', table_name='referrals')
    op.drop_index('ix_referrals_referrer_id', table_name='referrals')
    op.drop_table('referrals')
    
    # Drop referral_code column from users table
    op.drop_index('ix_users_referral_code', table_name='users')
    op.drop_constraint('uq_users_referral_code', 'users', type_='unique')
    op.drop_column('users', 'referral_code')
    
    print("⚠️  Referral system removed - rolled back migration")
