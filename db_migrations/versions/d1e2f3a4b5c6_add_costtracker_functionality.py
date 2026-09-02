"""add functionality bucket to costtracker

Revision ID: d1e2f3a4b5c6
Revises: c6d7e8f9a0b1
Create Date: 2026-07-16

Additive migration — adds a stable, user-facing "functionality" bucket to
``costtracker`` so the dashboard can group spend by product feature (chat,
report_generation, refine, refine_visualization, executive_summary,
infographic, pptx_generation, ask_caspr, other).

The column is a plain ``VARCHAR`` (NOT a Postgres native enum): the allowed
values live in ``src.core.observability.functionality_context.Functionality`` (a Python enum)
so buckets can be added / renamed without a DB enum migration.

A best-effort backfill maps existing rows' free-text ``context`` / ``agent_name``
labels onto the new buckets so historical spend is attributed too.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (SQL ILIKE pattern, functionality) — evaluated top-to-bottom; FIRST match
# wins. Kept in sync with src.core.observability.functionality_context._CONTEXT_RULES. The
# patterns match the human-readable ``context`` phrases stored by
# ``save_raw_llm_response`` (plus code-identifier fragments for robustness).
_BACKFILL_RULES: list[tuple[str, str]] = [
    # Report-gen call that seeds Ask CASPR — must precede the ask-caspr rule.
    ("%summary for ask caspr%", "report_generation"),
    # Ask CASPR Q&A.
    ("%ask caspr question%", "ask_caspr"),
    ("%ask_caspr%", "ask_caspr"),
    ("%askcaspr%", "ask_caspr"),
    # PPTX.
    ("%pptx%", "pptx_generation"),
    ("%presentation%", "pptx_generation"),
    # Executive-summary updater.
    ("%executive summary needs an update%", "executive_summary"),
    ("%checking whether the executive summary%", "executive_summary"),
    ("%updating the executive summary%", "executive_summary"),
    ("%executive summary after report changes%", "executive_summary"),
    ("%executive_summary_updater%", "executive_summary"),
    # Visualization refinement.
    ("%visualization based on feedback%", "refine_visualization"),
    ("%refined table visualization%", "refine_visualization"),
    ("%refined chart visualization%", "refine_visualization"),
    ("%regenerating a chart image%", "refine_visualization"),
    ("%refine_visualiz%", "refine_visualization"),
    ("%viz_refine%", "refine_visualization"),
    ("%visualizer_refine%", "refine_visualization"),
    ("%graph_maker_refine%", "refine_visualization"),
    ("%table_visualizer_refine%", "refine_visualization"),
    # Card refinement.
    ("%refining a report card%", "refine"),
    ("%after card edits%", "refine"),
    ("%refine_card%", "refine"),
    ("%refiner%", "refine"),
    # Infographic.
    ("%infographic%", "infographic"),
    ("%one-page%", "infographic"),
    ("%one_pager%", "infographic"),
    ("%illustration for the report%", "infographic"),
    # Interactive chat / planning agent.
    ("%main research request%", "chat"),
    ("%title for the chat%", "chat"),
    ("%latest information on the topic%", "chat"),
    ("%low-balance research request%", "chat"),
    ("%question from uploaded documents%", "chat"),
    ("%normal_flow%", "chat"),
    ("%retrieve_latest%", "chat"),
    ("%chat_title%", "chat"),
    ("%query_document%", "chat"),
    # Broad report-generation catch-all (evaluated last so specific buckets win).
    ("%report outline%", "report_generation"),
    ("%section summaries%", "report_generation"),
    ("%report section content%", "report_generation"),
    ("%research preview%", "report_generation"),
    ("%summarizing a report section%", "report_generation"),
    ("%overall report summary%", "report_generation"),
    ("%polishing the executive summary%", "report_generation"),
    ("%title for a table%", "report_generation"),
    ("%chart type for the data%", "report_generation"),
    ("%chart image from table data%", "report_generation"),
    ("%refining the report structure%", "report_generation"),
    ("%table should become a chart%", "report_generation"),
    ("%generated chart image%", "report_generation"),
    ("%writing code to draw a chart%", "report_generation"),
    ("%flowchart or diagram%", "report_generation"),
    ("%key findings from the report%", "report_generation"),
    ("%industry or sector%", "report_generation"),
    ("%dashboard highlights%", "report_generation"),
    ("%styling for the published report%", "report_generation"),
    ("%report poster image%", "report_generation"),
    ("%cover image for the report%", "report_generation"),
    ("%report text for publishing%", "report_generation"),
    ("%matching poster%", "report_generation"),
    ("%poster images for publishing%", "report_generation"),
    ("%document search%", "report_generation"),
    ("%search across documents%", "report_generation"),
    ("%search documents%", "report_generation"),
    ("%document context%", "report_generation"),
    ("%answer a question%", "report_generation"),
    ("%into an answer%", "report_generation"),
    ("%card_utils%", "report_generation"),
    ("%card_fixer%", "report_generation"),
    ("%viz_pipeline%", "report_generation"),
    ("%html_graph_maker%", "report_generation"),
    ("%html_table_visualizer%", "report_generation"),
    ("%poster%", "report_generation"),
    ("%publish%", "report_generation"),
    ("%grep%", "report_generation"),
    ("%content_processor%", "report_generation"),
    ("%generate_%", "report_generation"),
]


def upgrade() -> None:
    op.add_column(
        'costtracker',
        sa.Column(
            'functionality',
            sa.String(length=50),
            nullable=True,
            comment=(
                'Stable user-facing functionality bucket '
                '(see src.core.observability.functionality_context.Functionality), '
                'e.g. report_generation'
            ),
        ),
    )
    op.create_index(
        'idx_costtracker_functionality',
        'costtracker',
        ['functionality'],
        unique=False,
    )

    # Best-effort backfill of historical rows. Later rules only touch rows the
    # earlier (more specific) rules did not already claim.
    conn = op.get_bind()
    for pattern, functionality in _BACKFILL_RULES:
        conn.execute(
            sa.text(
                "UPDATE costtracker SET functionality = :func "
                "WHERE functionality IS NULL "
                "AND (COALESCE(context, '') || ' ' || COALESCE(agent_name, '')) ILIKE :pat"
            ),
            {"func": functionality, "pat": pattern},
        )
    # Anything still unmatched → 'other'.
    conn.execute(
        sa.text(
            "UPDATE costtracker SET functionality = 'other' WHERE functionality IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index('idx_costtracker_functionality', table_name='costtracker')
    op.drop_column('costtracker', 'functionality')
