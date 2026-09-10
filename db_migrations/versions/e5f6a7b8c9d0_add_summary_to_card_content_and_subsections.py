"""add summary key to card content and sub_sections JSONB

Revision ID: e5f6a7b8c9d0
Revises: c4d5e6f7a8b9
Create Date: 2026-02-27 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add "summary": "" key to the JSONB in cards.content and each element
    of cards.sub_sections for all existing rows that don't already have it.

    - content column stores a single JSON object  → merge {"summary": ""}
    - sub_sections column stores a JSON array of objects → merge {"summary": ""} into each element
    """

    # --- 1. Add summary to content (single JSONB object) ---
    op.execute(
        text("""
            UPDATE cards
            SET content = content || '{"summary": ""}'::jsonb
            WHERE content IS NOT NULL
              AND jsonb_typeof(content) = 'object'
              AND NOT content ? 'summary'
        """)
    )

    # --- 2. Add summary to each element in sub_sections (JSONB array) ---
    op.execute(
        text("""
            UPDATE cards
            SET sub_sections = (
                SELECT COALESCE(
                    jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(elem) = 'object' AND NOT (elem ? 'summary')
                            THEN elem || '{"summary": ""}'::jsonb
                            ELSE elem
                        END
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(sub_sections) AS elem
            )
            WHERE sub_sections IS NOT NULL
              AND jsonb_typeof(sub_sections) = 'array'
              AND jsonb_array_length(sub_sections) > 0
        """)
    )

    print("✅ Migration complete:")
    print("   - Added 'summary' key with empty string to cards.content JSONB objects")
    print(
        "   - Added 'summary' key with empty string to each element in cards.sub_sections JSONB arrays"
    )


def downgrade() -> None:
    """
    Remove the "summary" key from cards.content and each element of
    cards.sub_sections for all existing rows.
    """

    # --- 1. Remove summary from content (single JSONB object) ---
    op.execute(
        text("""
            UPDATE cards
            SET content = content - 'summary'
            WHERE content IS NOT NULL
              AND jsonb_typeof(content) = 'object'
              AND content ? 'summary'
        """)
    )

    # --- 2. Remove summary from each element in sub_sections (JSONB array) ---
    op.execute(
        text("""
            UPDATE cards
            SET sub_sections = (
                SELECT COALESCE(
                    jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(elem) = 'object' AND (elem ? 'summary')
                            THEN elem - 'summary'
                            ELSE elem
                        END
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(sub_sections) AS elem
            )
            WHERE sub_sections IS NOT NULL
              AND jsonb_typeof(sub_sections) = 'array'
              AND jsonb_array_length(sub_sections) > 0
        """)
    )

    print("✅ Rollback complete:")
    print("   - Removed 'summary' key from cards.content JSONB objects")
    print("   - Removed 'summary' key from each element in cards.sub_sections JSONB arrays")
