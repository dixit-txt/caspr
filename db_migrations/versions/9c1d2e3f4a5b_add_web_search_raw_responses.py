"""add web_search_raw_responses table

Revision ID: 9c1d2e3f4a5b
Revises: c2d3e4f5a6b7
Create Date: 2026-08-05 15:00:00.000000

One new table storing the full raw OpenAI Responses-API payload
(response.model_dump(mode='json')) for every web-search-enabled call made
during card generation, card refinement, and Ask Caspr. 1:1 with
web_search_events; kept separate so the large JSON body doesn't bloat the
hot event/citation tables used for routine analytics queries.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9c1d2e3f4a5b"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_search_raw_responses",
        sa.Column("id", sa.CHAR(36), nullable=False, comment="Primary key"),
        sa.Column(
            "search_event_id",
            sa.CHAR(36),
            nullable=False,
            comment="FK to web_search_events.id (1:1)",
        ),
        sa.Column(
            "operation_id", sa.CHAR(36), nullable=False, comment="Denormalized from parent event"
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.CHAR(36), nullable=True, comment="Denormalized FK to users.id"),
        sa.Column("report_id", sa.CHAR(36), nullable=True, comment="Denormalized FK to reports.id"),
        sa.Column(
            "chat_id",
            sa.String(255),
            nullable=True,
            comment="Client-generated chat session ID — not an FK",
        ),
        sa.Column(
            "card_id",
            sa.CHAR(36),
            nullable=True,
            comment="Logical card identifier; intentionally not an FK",
        ),
        sa.Column(
            "trigger_source",
            sa.String(30),
            nullable=False,
            comment="'card_generation', 'brief', 'card_refinement', or 'ask_caspr'",
        ),
        sa.Column("provider", sa.String(50), nullable=True, comment="'openai'"),
        sa.Column(
            "provider_response_id", sa.String(255), nullable=True, comment="OpenAI response.id"
        ),
        sa.Column("previous_response_id", sa.String(255), nullable=True),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column(
            "response_status",
            sa.String(30),
            nullable=True,
            comment="response.status — 'completed', 'incomplete', 'failed', etc.",
        ),
        sa.Column(
            "raw_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Full response.model_dump(mode='json')",
        ),
        sa.Column("payload_size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "is_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True if raw_response was truncated due to the size guard",
        ),
        sa.Column(
            "response_created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="OpenAI's response.created_at (epoch converted to UTC)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["search_event_id"], ["web_search_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("search_event_id", name="uq_web_search_raw_responses_search_event_id"),
    )

    op.create_index(
        "idx_web_search_raw_responses_search_event_id",
        "web_search_raw_responses",
        ["search_event_id"],
        unique=True,
    )
    op.create_index(
        "idx_web_search_raw_responses_operation_id", "web_search_raw_responses", ["operation_id"]
    )
    op.create_index(
        "idx_web_search_raw_responses_provider_response_id",
        "web_search_raw_responses",
        ["provider_response_id"],
    )
    op.create_index("idx_web_search_raw_responses_user_id", "web_search_raw_responses", ["user_id"])
    op.create_index(
        "idx_web_search_raw_responses_report_id", "web_search_raw_responses", ["report_id"]
    )
    op.create_index(
        "idx_web_search_raw_responses_trigger_source",
        "web_search_raw_responses",
        ["trigger_source"],
    )
    op.create_index(
        "idx_web_search_raw_responses_created_at", "web_search_raw_responses", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_web_search_raw_responses_created_at", table_name="web_search_raw_responses")
    op.drop_index(
        "idx_web_search_raw_responses_trigger_source", table_name="web_search_raw_responses"
    )
    op.drop_index("idx_web_search_raw_responses_report_id", table_name="web_search_raw_responses")
    op.drop_index("idx_web_search_raw_responses_user_id", table_name="web_search_raw_responses")
    op.drop_index(
        "idx_web_search_raw_responses_provider_response_id", table_name="web_search_raw_responses"
    )
    op.drop_index(
        "idx_web_search_raw_responses_operation_id", table_name="web_search_raw_responses"
    )
    op.drop_index(
        "idx_web_search_raw_responses_search_event_id", table_name="web_search_raw_responses"
    )
    op.drop_table("web_search_raw_responses")
