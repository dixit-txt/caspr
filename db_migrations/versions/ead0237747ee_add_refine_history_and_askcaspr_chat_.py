"""add refine history and askcaspr chat table in DB

Revision ID: ead0237747ee
Revises: fix_invalidated_case
Create Date: 2026-02-10 12:27:16.143147

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ead0237747ee"
down_revision: str | None = "fix_invalidated_case"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create ask_caspr_chats table
    op.create_table(
        "ask_caspr_chats",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column(
            "report_id",
            sa.CHAR(length=36),
            nullable=False,
            comment="Reference to the report (stable anchor)",
        ),
        sa.Column(
            "section_id",
            sa.CHAR(length=36),
            nullable=False,
            comment="Business card_id from cards.card_id — does not change across versions",
        ),
        sa.Column(
            "subsection_id",
            sa.CHAR(length=36),
            nullable=True,
            comment="Subsection id — NULL for section-level chats, does not change across versions",
        ),
        sa.Column(
            "chat",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Ask Caspr conversation as JSON array of {role, content} messages",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="Version number of the section/subsection content when this chat entry was created",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Used to determine the current/latest chat entry",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When chat was last updated",
        ),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index(
        "idx_ask_caspr_chats_created_at", "ask_caspr_chats", ["created_at"], unique=False
    )
    op.create_index(
        "idx_ask_caspr_chats_latest_lookup",
        "ask_caspr_chats",
        ["report_id", "section_id", "subsection_id", "created_at"],
        unique=False,
    )
    op.create_index("idx_ask_caspr_chats_report_id", "ask_caspr_chats", ["report_id"], unique=False)
    op.create_index(
        "idx_ask_caspr_chats_section_id", "ask_caspr_chats", ["section_id"], unique=False
    )
    op.create_index(
        "idx_ask_caspr_chats_subsection_id", "ask_caspr_chats", ["subsection_id"], unique=False
    )

    # Create refinement_history table
    op.create_table(
        "refinement_history",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column(
            "report_id",
            sa.CHAR(length=36),
            nullable=False,
            comment="Reference to the report (one history per report)",
        ),
        sa.Column(
            "refine_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="JSON array storing refinement history",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint("report_id"),
    )
    op.create_index(
        "idx_refinement_history_report_id", "refinement_history", ["report_id"], unique=False
    )

    # Add file_id column to reports table
    op.add_column(
        "reports",
        sa.Column(
            "file_id", sa.String(length=200), nullable=True, comment="OpenAI file ID for the report"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop file_id column from reports table
    op.drop_column("reports", "file_id")

    # Drop refinement_history
    op.drop_index("idx_refinement_history_report_id", table_name="refinement_history")
    op.drop_table("refinement_history")

    # Drop ask_caspr_chats
    op.drop_index("idx_ask_caspr_chats_subsection_id", table_name="ask_caspr_chats")
    op.drop_index("idx_ask_caspr_chats_section_id", table_name="ask_caspr_chats")
    op.drop_index("idx_ask_caspr_chats_report_id", table_name="ask_caspr_chats")
    op.drop_index("idx_ask_caspr_chats_latest_lookup", table_name="ask_caspr_chats")
    op.drop_index("idx_ask_caspr_chats_created_at", table_name="ask_caspr_chats")
    op.drop_table("ask_caspr_chats")
