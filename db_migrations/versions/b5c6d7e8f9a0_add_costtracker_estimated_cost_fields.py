"""add estimated_cost and cost_details to costtracker

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-15

Additive migration — stores priced cost from ``llm_cost_calculator``:
  estimated_cost (float, nullable)  ← payload.cost.estimated_cost_usd
  cost_details   (JSONB, nullable)  ← full payload.cost block
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "costtracker",
        sa.Column(
            "estimated_cost",
            sa.Float(),
            nullable=True,
            comment="Estimated USD cost for this LLM call",
        ),
    )
    op.add_column(
        "costtracker",
        sa.Column(
            "cost_details",
            JSONB(),
            nullable=True,
            comment="Full cost breakdown from llm_cost_calculator (payload cost block)",
        ),
    )
    op.create_index(
        "idx_costtracker_estimated_cost",
        "costtracker",
        ["estimated_cost"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_costtracker_estimated_cost", table_name="costtracker")
    op.drop_column("costtracker", "cost_details")
    op.drop_column("costtracker", "estimated_cost")
