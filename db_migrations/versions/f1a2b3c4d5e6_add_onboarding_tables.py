"""add onboarding tables and user columns

Creates the onboarding system tables:
  - user_roles: lookup for "What best describes you?"
  - research_interests: lookup for "What do you want to research?"
  - user_research_interests: junction (many-to-many users <-> research_interests)
  - universities: partner university domains for student discount

Adds columns to users table:
  - user_role_id (FK -> user_roles.id)
  - university_email
  - is_university_verified
  - university_id (FK -> universities.id)
  - onboarding_completed

Seeds initial rows into user_roles and research_interests.

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-04-10 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from uuid_utils import uuid7

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. user_roles
    # ------------------------------------------------------------------
    op.create_table(
        "user_roles",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(100), unique=True, nullable=False, comment="Internal key"),
        sa.Column("display_name", sa.String(255), nullable=False, comment="UI label"),
        sa.Column("description", sa.String(500), nullable=True, comment="Subtitle in UI card"),
        sa.Column("discount_tag", sa.String(50), nullable=True, comment="Badge text e.g. 50% OFF!"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("idx_user_roles_name", "user_roles", ["name"])
    op.create_index("idx_user_roles_is_active", "user_roles", ["is_active"])

    # ------------------------------------------------------------------
    # 2. research_interests
    # ------------------------------------------------------------------
    op.create_table(
        "research_interests",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(100), unique=True, nullable=False, comment="Internal key"),
        sa.Column("display_name", sa.String(255), nullable=False, comment="UI label"),
        sa.Column("description", sa.String(500), nullable=True, comment="Subtitle in UI card"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("idx_research_interests_name", "research_interests", ["name"])
    op.create_index("idx_research_interests_is_active", "research_interests", ["is_active"])

    # ------------------------------------------------------------------
    # 3. universities
    # ------------------------------------------------------------------
    op.create_table(
        "universities",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False, comment="University name"),
        sa.Column(
            "email_domain", sa.String(255), unique=True, nullable=False, comment="e.g. iitb.ac.in"
        ),
        sa.Column(
            "discount_percentage", sa.Integer(), nullable=False, server_default=sa.text("50")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("idx_universities_email_domain", "universities", ["email_domain"])
    op.create_index("idx_universities_is_active", "universities", ["is_active"])

    # ------------------------------------------------------------------
    # 4. user_research_interests (junction)
    # ------------------------------------------------------------------
    op.create_table(
        "user_research_interests",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.CHAR(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "research_interest_id",
            sa.CHAR(36),
            sa.ForeignKey("research_interests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "research_interest_id", name="uq_user_research_interest"),
    )
    op.create_index("idx_user_research_interests_user_id", "user_research_interests", ["user_id"])
    op.create_index(
        "idx_user_research_interests_interest_id",
        "user_research_interests",
        ["research_interest_id"],
    )

    # ------------------------------------------------------------------
    # 5. New columns on users
    # ------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "user_role_id",
            sa.CHAR(36),
            sa.ForeignKey("user_roles.id", ondelete="SET NULL"),
            nullable=True,
            comment="Selected role from onboarding",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "university_email",
            sa.String(255),
            nullable=True,
            comment="University email for student verification",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_university_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether the university email has been verified",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "university_id",
            sa.CHAR(36),
            sa.ForeignKey("universities.id", ondelete="SET NULL"),
            nullable=True,
            comment="Partner university after domain validation",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "onboarding_completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether the user finished the onboarding flow",
        ),
    )

    op.create_index("idx_users_user_role_id", "users", ["user_role_id"])
    op.create_index("idx_users_university_id", "users", ["university_id"])
    op.create_index("idx_users_onboarding_completed", "users", ["onboarding_completed"])

    # ------------------------------------------------------------------
    # 6. Seed user_roles
    # ------------------------------------------------------------------
    roles = [
        (
            "founder_entrepreneur",
            "Founder / Entrepreneur",
            "Validating ideas and understanding markets",
            None,
            1,
        ),
        ("consultant", "Consultant", "Creating insights and reports for clients", None, 2),
        (
            "investor_vc_analyst",
            "Investor / VC / Analyst",
            "Evaluating markets, startups, and industries",
            None,
            3,
        ),
        (
            "marketing_growth",
            "Marketing / Growth",
            "Understanding customers and market trends",
            None,
            4,
        ),
        (
            "market_researcher_analyst",
            "Market Researcher / Analyst",
            "Running research and producing insights",
            None,
            5,
        ),
        (
            "corporate_strategy_business_ops",
            "Corporate Strategy / Business Ops",
            "Strategic planning and competitive intelligence",
            None,
            6,
        ),
        (
            "student_academic_researcher",
            "Student / Academic Researcher",
            "Learning and conducting academic research",
            "50% OFF!",
            7,
        ),
        ("other", "Other", "Custom", None, 8),
    ]
    user_roles_table = sa.table(
        "user_roles",
        sa.column("id", sa.CHAR(36)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.String),
        sa.column("discount_tag", sa.String),
        sa.column("display_order", sa.Integer),
    )
    op.bulk_insert(
        user_roles_table,
        [
            {
                "id": str(uuid7()),
                "name": r[0],
                "display_name": r[1],
                "description": r[2],
                "discount_tag": r[3],
                "display_order": r[4],
            }
            for r in roles
        ],
    )

    # ------------------------------------------------------------------
    # 7. Seed research_interests
    # ------------------------------------------------------------------
    interests = [
        ("market_research", "Market Research", "Understand market size, trends, and dynamics", 1),
        (
            "competitive_analysis",
            "Competitive Analysis",
            "Analyze competitors, positioning, and strategies",
            2,
        ),
        (
            "startup_idea_validation",
            "Startup / Idea Validation",
            "Test business ideas and opportunities",
            3,
        ),
        ("industry_research", "Industry Research", "Deep dive into industries and sectors", 4),
        ("research_reports", "Research Reports", "Create reports, presentations, or briefs", 5),
        ("trend_discovery", "Trend Discovery", "Identify emerging trends and opportunities", 6),
        ("quick_insights", "Quick Insights", "Get fast summaries and answers", 7),
        ("something_else", "Something else", "Custom", 8),
    ]
    research_interests_table = sa.table(
        "research_interests",
        sa.column("id", sa.CHAR(36)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.String),
        sa.column("display_order", sa.Integer),
    )
    op.bulk_insert(
        research_interests_table,
        [
            {
                "id": str(uuid7()),
                "name": i[0],
                "display_name": i[1],
                "description": i[2],
                "display_order": i[3],
            }
            for i in interests
        ],
    )


def downgrade() -> None:
    op.drop_index("idx_users_onboarding_completed", table_name="users")
    op.drop_index("idx_users_university_id", table_name="users")
    op.drop_index("idx_users_user_role_id", table_name="users")

    op.drop_column("users", "onboarding_completed")
    op.drop_column("users", "university_id")
    op.drop_column("users", "is_university_verified")
    op.drop_column("users", "university_email")
    op.drop_column("users", "user_role_id")

    op.drop_table("user_research_interests")
    op.drop_table("universities")
    op.drop_table("research_interests")
    op.drop_table("user_roles")
