"""add updated_at to cards

Revision ID: d4e8f5a1c3b4
Revises: c3f7a8d9e1b2
Create Date: 2025-12-23 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = 'd4e8f5a1c3b4'
down_revision = '0e37808ea016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add updated_at column to cards table.
    
    For existing records, set updated_at = created_at to maintain backward compatibility.
    For new records, updated_at will be set to current timestamp by default.
    
    This field will be used to track when a card was last modified, particularly for:
    - Visualization refinements (refine-visualization)
    - Visualization deletions (delete-visualization)
    
    The detect_modified_cards() function will use updated_at to determine if cards
    have been modified since the last report generation.
    """
    
    # Step 1: Add updated_at column (nullable initially to allow backfill)
    op.add_column('cards', 
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True)
    )
    
    # Step 2: Backfill existing records - set updated_at = created_at
    op.execute(
        text("UPDATE cards SET updated_at = created_at WHERE updated_at IS NULL")
    )
    
    # Step 3: Make column NOT NULL with server default for new inserts
    op.alter_column('cards', 'updated_at',
        nullable=False,
        server_default=sa.text('CURRENT_TIMESTAMP')
    )
    
    print("✅ Migration complete:")
    print("   - Added updated_at column to cards table")
    print("   - Backfilled existing records with updated_at = created_at")
    print("   - Set default to CURRENT_TIMESTAMP for new records")


def downgrade() -> None:
    """
    Remove updated_at column from cards table.
    """
    op.drop_column('cards', 'updated_at')
    print("✅ Rollback complete: Removed updated_at column from cards table")

