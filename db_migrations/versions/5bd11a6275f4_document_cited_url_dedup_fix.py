"""document cited-url dedup fix on web_search_citations

Revision ID: 5bd11a6275f4
Revises: 9c1d2e3f4a5b
Create Date: 2026-08-06 12:20:00.000000

No schema change. Adds Postgres COMMENT ON TABLE/COLUMN metadata to
web_search_citations and web_search_events.cited_count, documenting a bug
(now fixed in app.admin.repository_web_search.py::_normalize_links) where the same
cited URL could be persisted as two was_cited_in_output=True rows per
search_event_id — one sourced from OpenAI's own url_citation annotations,
one from a plain-text URL regex fallback over the final rendered output.

These comments are intended to give any tool or LLM introspecting the
schema directly (e.g. for analysis or dashboards) the same context as the
Python-side docstrings on WebSearchCitation / WebSearchEvent, so historical
rows created before this fix aren't silently double-counted.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5bd11a6275f4"
down_revision: str | None = "9c1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE_COMMENT = (
    "One row per URL returned by a single WebSearchEvent (candidate or cited). "
    "CAVEAT for analysis/dashboards: rows created before the dedup fix in "
    "app.admin.repository_web_search.py::_normalize_links may contain the SAME cited URL "
    "as two separate was_cited_in_output=true rows per search_event_id (one "
    "from OpenAI's url_citation annotations, one from a text-regex fallback "
    "over the final rendered output). This does not affect "
    "was_cited_in_output=false (candidate) rows, which can legitimately repeat "
    "a URL across different search_call_index/result_rank values by design. "
    "Prefer COUNT(DISTINCT url) / GROUP BY url per search_event_id over raw "
    "COUNT(*) when counting or listing cited sources."
)

_CITED_COUNT_COMMENT = (
    "Count of WebSearchCitation rows with was_cited_in_output=true for this "
    "event. For events created before the dedup fix described on "
    "web_search_citations, treat as an upper bound rather than an exact count "
    "of distinct cited sources (this column was not backfilled)."
)


def upgrade() -> None:
    op.execute(f"COMMENT ON TABLE web_search_citations IS {_pg_quote(_TABLE_COMMENT)}")
    op.execute(
        f"COMMENT ON COLUMN web_search_events.cited_count IS {_pg_quote(_CITED_COUNT_COMMENT)}"
    )


def downgrade() -> None:
    op.execute("COMMENT ON TABLE web_search_citations IS NULL")
    op.execute("COMMENT ON COLUMN web_search_events.cited_count IS NULL")


def _pg_quote(text: str) -> str:
    """Escape a string as a Postgres single-quoted literal."""
    return "'" + text.replace("'", "''") + "'"
