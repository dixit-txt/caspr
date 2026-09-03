"""add/normalize mcp_api_keys table (one long-lived MCP key per user)

Revision ID: a9b0c1d2e3f4
Revises: 8b9c0d1e2f3a
Create Date: 2026-07-24

Idempotent: an earlier experimental ``mcp_api_keys`` table may already exist
with a different shape (name/revoked_at/is_active). This revision normalizes
it to the production schema used by the API.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "8b9c0d1e2f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(sa.text("SELECT to_regclass('public.mcp_api_keys') IS NOT NULL")).scalar()

    if not exists:
        op.create_table(
            "mcp_api_keys",
            sa.Column("id", sa.CHAR(length=36), nullable=False),
            sa.Column(
                "user_id",
                sa.CHAR(length=36),
                nullable=False,
                comment="Owning user (exactly one MCP API key per user)",
            ),
            sa.Column(
                "key_prefix",
                sa.String(length=24),
                nullable=False,
                comment="Non-secret prefix for UI display",
            ),
            sa.Column(
                "key_hash",
                sa.String(length=64),
                nullable=False,
                comment="HMAC-SHA256 hex digest of the key",
            ),
            sa.Column(
                "last_four",
                sa.String(length=4),
                nullable=False,
                comment="Last 4 characters of the plaintext key",
            ),
            sa.Column(
                "last_used_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="Last successful auth with this key",
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
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", name="uq_mcp_api_keys_user_id"),
            sa.UniqueConstraint("key_hash", name="uq_mcp_api_keys_key_hash"),
        )
        op.create_index("ix_mcp_api_keys_key_hash", "mcp_api_keys", ["key_hash"], unique=False)
        return

    # Normalize legacy experimental table → production schema.
    # Old plaintext keys cannot be recovered; wipe rows that lack display fields.
    op.execute(sa.text("ALTER TABLE mcp_api_keys ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(24)"))
    op.execute(sa.text("ALTER TABLE mcp_api_keys ADD COLUMN IF NOT EXISTS last_four VARCHAR(4)"))
    op.execute(sa.text("DELETE FROM mcp_api_keys WHERE key_prefix IS NULL OR last_four IS NULL"))
    op.execute(sa.text("ALTER TABLE mcp_api_keys ALTER COLUMN key_prefix SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE mcp_api_keys ALTER COLUMN last_four SET NOT NULL"))

    # Drop legacy columns if present.
    op.execute(sa.text("ALTER TABLE mcp_api_keys DROP COLUMN IF EXISTS name"))
    op.execute(sa.text("ALTER TABLE mcp_api_keys DROP COLUMN IF EXISTS revoked_at"))
    op.execute(sa.text("ALTER TABLE mcp_api_keys DROP COLUMN IF EXISTS is_active"))

    # Enforce one key per user (keep newest if duplicates exist).
    op.execute(
        sa.text(
            """
            DELETE FROM mcp_api_keys a
            USING mcp_api_keys b
            WHERE a.user_id = b.user_id
              AND a.created_at < b.created_at
            """
        )
    )

    # Unique constraints / index (ignore if already present).
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                ALTER TABLE mcp_api_keys
                    ADD CONSTRAINT uq_mcp_api_keys_user_id UNIQUE (user_id);
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                ALTER TABLE mcp_api_keys
                    ADD CONSTRAINT uq_mcp_api_keys_key_hash UNIQUE (key_hash);
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE INDEX ix_mcp_api_keys_key_hash ON mcp_api_keys (key_hash);
            EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
            END $$
            """
        )
    )


def downgrade() -> None:
    op.drop_table("mcp_api_keys")
