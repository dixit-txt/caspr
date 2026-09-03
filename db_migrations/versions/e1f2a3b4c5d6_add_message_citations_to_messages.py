"""add message_citations to messages table

Revision ID: e1f2a3b4c5d6
Revises: c1d2e3f4a5b6
Create Date: 2026-07-02 15:00:00.000000

Add a message_citations JSONB column to the messages table to persist
citations returned by retrieve_latest_info and query_document tool calls.

Structure: { "<langchain_ai_message_id>": ["url1", "url2", ...] }

Each key is the LangChain AIMessage.id of the assistant turn that triggered
the tool call. The value is the de-duplicated list of source URLs cited in
that response. One chat can have multiple turns each with their own key.

Old rows will have NULL which the API treats as no citations.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add message_citations JSONB column to messages table.

    The column is nullable so all existing rows default to NULL without
    requiring a backfill. The application treats NULL as an empty dict.
    """
    op.add_column(
        "messages",
        sa.Column(
            "message_citations",
            JSONB,
            nullable=True,
            comment=(
                "Persisted citations keyed by LangChain AIMessage id. "
                'Structure: {"<ai_message_id>": ["url1", "url2", ...]}. '
                "NULL for chats created before this migration."
            ),
        ),
    )

    print("Migration complete: added message_citations (JSONB, nullable) to messages table")


def downgrade() -> None:
    """Remove message_citations column from messages table."""
    op.drop_column("messages", "message_citations")
    print("Rollback complete: removed message_citations from messages table")
