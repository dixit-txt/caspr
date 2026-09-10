"""extend web search analytics metadata

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-07-22 20:50:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8b9c0d1e2f3a"
down_revision: str | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EVENT_COLUMNS = (
    sa.Column("operation_id", sa.CHAR(36), nullable=True),
    sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("provider", sa.String(50), nullable=True),
    sa.Column("provider_response_id", sa.String(255), nullable=True),
    sa.Column("provider_queries", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("status", sa.String(30), nullable=True),
    sa.Column("error_type", sa.String(255), nullable=True),
    sa.Column("duration_ms", sa.Integer(), nullable=True),
    sa.Column("search_call_count", sa.Integer(), nullable=True, server_default=sa.text("0")),
    sa.Column("candidate_count", sa.Integer(), nullable=True, server_default=sa.text("0")),
    sa.Column("cited_count", sa.Integer(), nullable=True, server_default=sa.text("0")),
    sa.Column("usage_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("card_id", sa.CHAR(36), nullable=True),
)

CITATION_COLUMNS = (
    sa.Column("raw_url", sa.Text(), nullable=True),
    sa.Column("search_call_id", sa.String(255), nullable=True),
    sa.Column("search_call_index", sa.Integer(), nullable=True),
    sa.Column("result_rank", sa.Integer(), nullable=True),
    sa.Column("citation_order", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    for column in EVENT_COLUMNS:
        op.add_column("web_search_events", column)

    # Existing rows get a stable correlation value without inventing a shared
    # operation. New ORM writes generate a fresh operation UUID.
    op.execute("UPDATE web_search_events SET operation_id = id WHERE operation_id IS NULL")
    op.alter_column("web_search_events", "operation_id", nullable=False)

    for column in CITATION_COLUMNS:
        op.add_column("web_search_citations", column)

    op.create_index(
        "idx_web_search_events_operation_id",
        "web_search_events",
        ["operation_id"],
    )
    op.create_index("idx_web_search_events_provider", "web_search_events", ["provider"])
    op.create_index("idx_web_search_events_status", "web_search_events", ["status"])
    op.create_index("idx_web_search_events_card_id", "web_search_events", ["card_id"])
    op.create_index(
        "idx_web_search_citations_was_cited",
        "web_search_citations",
        ["was_cited_in_output"],
    )
    op.create_index(
        "idx_web_search_citations_event_role_rank",
        "web_search_citations",
        [
            "search_event_id",
            "was_cited_in_output",
            "search_call_index",
            "result_rank",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_web_search_citations_event_role_rank",
        table_name="web_search_citations",
    )
    op.drop_index(
        "idx_web_search_citations_was_cited",
        table_name="web_search_citations",
    )
    op.drop_index("idx_web_search_events_card_id", table_name="web_search_events")
    op.drop_index("idx_web_search_events_status", table_name="web_search_events")
    op.drop_index("idx_web_search_events_provider", table_name="web_search_events")
    op.drop_index("idx_web_search_events_operation_id", table_name="web_search_events")

    for column in reversed(CITATION_COLUMNS):
        op.drop_column("web_search_citations", column.name)
    for column in reversed(EVENT_COLUMNS):
        op.drop_column("web_search_events", column.name)
