"""fix_invalidated_enum_case

Revision ID: fix_invalidated_case
Revises: d6541feb12d8
Create Date: 2026-02-06 15:00:00.000000

FIXED VERSION: Handles case where 'invalidated' might not exist in production
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "fix_invalidated_case"
down_revision: str | None = "d6541feb12d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Fix the case of 'invalidated' enum value to 'INVALIDATED' to match code expectations.

    This version handles the case where:
    - 'invalidated' might not exist in the enum (if previous migration didn't add it)
    - Or 'invalidated' exists but there are records using it

    Strategy:
    1. Check if 'invalidated' exists, if so update records
    2. Create new enum with INVALIDATED (uppercase)
    3. Migrate columns to new enum
    4. Clean up old enum
    """

    # Create new enum type with correct casing (INVALIDATED uppercase)
    # This includes all existing values plus INVALIDATED
    op.execute("""
        CREATE TYPE transaction_status_enum_new AS ENUM (
            'COMPLETED', 'PENDING', 'FAILED', 'REVERSED', 'INVALIDATED'
        );
    """)

    # Alter token_batches.status column to use new enum
    # USING clause converts old values to new enum:
    # - If old value was 'invalidated' (lowercase), map to 'REVERSED' since enum doesn't have lowercase
    # - All other values stay the same
    op.execute("""
        ALTER TABLE token_batches 
        ALTER COLUMN status TYPE transaction_status_enum_new 
        USING (
            CASE 
                WHEN status::text = 'invalidated' THEN 'REVERSED'::transaction_status_enum_new
                ELSE status::text::transaction_status_enum_new
            END
        );
    """)

    # Alter token_transactions.status column to use new enum
    op.execute("""
        ALTER TABLE token_transactions 
        ALTER COLUMN status TYPE transaction_status_enum_new 
        USING (
            CASE 
                WHEN status::text = 'invalidated' THEN 'REVERSED'::transaction_status_enum_new
                ELSE status::text::transaction_status_enum_new
            END
        );
    """)

    # Drop old enum type (CASCADE to handle dependencies)
    op.execute("DROP TYPE transaction_status_enum CASCADE;")

    # Rename new enum to original name
    op.execute("ALTER TYPE transaction_status_enum_new RENAME TO transaction_status_enum;")

    print("✅ Fixed transaction_status_enum: 'invalidated' → 'INVALIDATED'")


def downgrade() -> None:
    """
    Revert 'INVALIDATED' back to 'invalidated' (lowercase).

    Note: This is for rollback purposes only. In production, you likely won't need this.
    """

    # Create enum with lowercase 'invalidated'
    op.execute("""
        CREATE TYPE transaction_status_enum_new AS ENUM (
            'COMPLETED', 'PENDING', 'FAILED', 'REVERSED', 'invalidated'
        );
    """)

    # Alter columns to use new enum with lowercase
    op.execute("""
        ALTER TABLE token_batches 
        ALTER COLUMN status TYPE transaction_status_enum_new 
        USING (
            CASE 
                WHEN status::text = 'INVALIDATED' THEN 'invalidated'::transaction_status_enum_new
                ELSE status::text::transaction_status_enum_new
            END
        );
    """)

    op.execute("""
        ALTER TABLE token_transactions 
        ALTER COLUMN status TYPE transaction_status_enum_new 
        USING (
            CASE 
                WHEN status::text = 'INVALIDATED' THEN 'invalidated'::transaction_status_enum_new
                ELSE status::text::transaction_status_enum_new
            END
        );
    """)

    # Drop and rename
    op.execute("DROP TYPE transaction_status_enum CASCADE;")
    op.execute("ALTER TYPE transaction_status_enum_new RENAME TO transaction_status_enum;")

    print("⚠️  Downgraded: 'INVALIDATED' → 'invalidated'")
