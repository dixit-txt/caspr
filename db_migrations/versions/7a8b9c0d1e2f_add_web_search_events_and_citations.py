"""add web_search_events and web_search_citations tables

Revision ID: 7a8b9c0d1e2f
Revises: d1e2f3a4b5c6
Create Date: 2026-07-17 13:44:00.000000

Two new tables that record every OpenAI web-search invocation and each URL
it returned.  Used for source analytics, citation quality analysis, and
domain-level trust scoring in later stages.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "7a8b9c0d1e2f"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # web_search_events                                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "web_search_events",
        sa.Column("id", sa.CHAR(36), nullable=False, comment="Primary key"),
        sa.Column("user_id", sa.CHAR(36), nullable=True, comment="FK to users.id"),
        sa.Column(
            "chat_id",
            sa.String(255),
            nullable=True,
            comment="Client-generated chat session ID — not an FK",
        ),
        sa.Column("report_id", sa.CHAR(36), nullable=True, comment="FK to reports.id (nullable)"),
        sa.Column(
            "trigger_source",
            sa.String(30),
            nullable=False,
            comment="'chat', 'card_generation', or 'brief'",
        ),
        sa.Column(
            "section_name",
            sa.Text(),
            nullable=True,
            comment="Report section name (card gen / brief only)",
        ),
        sa.Column(
            "user_query", sa.Text(), nullable=True, comment="Query string sent to the web search"
        ),
        sa.Column(
            "model_used", sa.String(50), nullable=True, comment="OpenAI model, e.g. gpt-4o, gpt-5.4"
        ),
        sa.Column(
            "total_results_count", sa.Integer(), nullable=True, comment="Number of URLs returned"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        # chat_id is intentionally NOT a FK — it holds the client-generated session
        # ID (e.g. 'chat-abc123'), which is not stored in the messages table.
    )

    op.create_index("idx_web_search_events_user_id", "web_search_events", ["user_id"])
    op.create_index("idx_web_search_events_chat_id", "web_search_events", ["chat_id"])
    op.create_index("idx_web_search_events_report_id", "web_search_events", ["report_id"])
    op.create_index("idx_web_search_events_trigger_source", "web_search_events", ["trigger_source"])
    op.create_index("idx_web_search_events_created_at", "web_search_events", ["created_at"])

    # ------------------------------------------------------------------ #
    # web_search_citations                                                 #
    # ------------------------------------------------------------------ #
    op.create_table(
        "web_search_citations",
        sa.Column("id", sa.CHAR(36), nullable=False, comment="Primary key"),
        sa.Column(
            "search_event_id", sa.CHAR(36), nullable=False, comment="FK to web_search_events.id"
        ),
        sa.Column("user_id", sa.CHAR(36), nullable=True, comment="Denormalized FK to users.id"),
        sa.Column("report_id", sa.CHAR(36), nullable=True, comment="Denormalized FK to reports.id"),
        sa.Column("url", sa.Text(), nullable=False, comment="Full cleaned URL"),
        sa.Column(
            "domain", sa.String(255), nullable=True, comment="Extracted hostname, e.g. reuters.com"
        ),
        sa.Column("title", sa.Text(), nullable=True, comment="Page title from search result"),
        sa.Column("snippet", sa.Text(), nullable=True, comment="Text snippet from search result"),
        sa.Column(
            "was_cited_in_output",
            sa.Boolean(),
            nullable=True,
            comment="True if URL cited inline in final output",
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
    )

    op.create_index(
        "idx_web_search_citations_search_event_id", "web_search_citations", ["search_event_id"]
    )
    op.create_index("idx_web_search_citations_user_id", "web_search_citations", ["user_id"])
    op.create_index("idx_web_search_citations_report_id", "web_search_citations", ["report_id"])
    op.create_index("idx_web_search_citations_domain", "web_search_citations", ["domain"])


def downgrade() -> None:
    op.drop_index("idx_web_search_citations_domain", table_name="web_search_citations")
    op.drop_index("idx_web_search_citations_report_id", table_name="web_search_citations")
    op.drop_index("idx_web_search_citations_user_id", table_name="web_search_citations")
    op.drop_index("idx_web_search_citations_search_event_id", table_name="web_search_citations")
    op.drop_table("web_search_citations")

    op.drop_index("idx_web_search_events_created_at", table_name="web_search_events")
    op.drop_index("idx_web_search_events_trigger_source", table_name="web_search_events")
    op.drop_index("idx_web_search_events_report_id", table_name="web_search_events")
    op.drop_index("idx_web_search_events_chat_id", table_name="web_search_events")
    op.drop_index("idx_web_search_events_user_id", table_name="web_search_events")
    op.drop_table("web_search_events")
