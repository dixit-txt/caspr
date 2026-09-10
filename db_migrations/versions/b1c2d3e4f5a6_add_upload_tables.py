"""add upload tables: user_vector_stores, uploaded_files, file_versions, vector_store_files, chat_files

Revision ID: b1c2d3e4f5a6
Revises: a7f3e9c2d1b8
Create Date: 2026-02-24 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a7f3e9c2d1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── PostgreSQL ENUM definitions ──────────────────────────────────────────
# Using postgresql.ENUM with create_type=False so that op.create_table
# does NOT auto-create the types; we manage creation/deletion explicitly
# via raw SQL to safely handle retries after partial failures.

uploaded_file_status_enum = PG_ENUM(
    "PENDING_OPENAI_UPLOAD",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "S3_FILE_MISSING",
    "EXPIRED",
    name="uploaded_file_status_enum",
    create_type=False,
)

file_upload_context_enum = PG_ENUM(
    "IN_CHAT",
    "STANDALONE",
    name="file_upload_context_enum",
    create_type=False,
)

file_usage_type_enum = PG_ENUM(
    "NEW_UPLOAD",
    "REFERENCE",
    name="file_usage_type_enum",
    create_type=False,
)


def upgrade() -> None:
    """Create upload-related enum types, tables, and indexes."""

    # ── Create enum types via raw SQL (idempotent: handles retries) ───
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE uploaded_file_status_enum AS ENUM (
                'PENDING_OPENAI_UPLOAD', 'PROCESSING', 'COMPLETED', 'FAILED',
                'S3_FILE_MISSING', 'EXPIRED'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE file_upload_context_enum AS ENUM ('IN_CHAT', 'STANDALONE');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE file_usage_type_enum AS ENUM ('NEW_UPLOAD', 'REFERENCE');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # ------------------------------------------------------------------ #
    # 1. user_vector_stores — one vector store per user
    # ------------------------------------------------------------------ #
    op.create_table(
        "user_vector_stores",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.CHAR(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
            comment="Reference to user (one vector store per user)",
        ),
        sa.Column(
            "vector_store_id",
            sa.String(200),
            nullable=True,
            comment="OpenAI vector store ID (null if not created yet or deleted)",
        ),
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last time this vector store was accessed/used",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_vector_stores_user_id"),
    )
    op.create_index("idx_user_vector_stores_user_id", "user_vector_stores", ["user_id"])

    # ------------------------------------------------------------------ #
    # 2. uploaded_files — metadata for every uploaded file
    # ------------------------------------------------------------------ #
    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.CHAR(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to user who uploaded the file",
        ),
        sa.Column(
            "s3_uri",
            sa.String(500),
            nullable=False,
            comment="S3 path of the uploaded file (for re-upload)",
        ),
        sa.Column(
            "original_filename",
            sa.String(255),
            nullable=True,
            comment="Original filename when uploaded",
        ),
        sa.Column("file_size", sa.Integer, nullable=True, comment="File size in bytes"),
        sa.Column(
            "file_type",
            sa.String(50),
            nullable=True,
            comment="File type/extension (pdf, docx, txt, etc.)",
        ),
        sa.Column(
            "content_hash",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hash for duplicate detection",
        ),
        sa.Column(
            "upload_context",
            file_upload_context_enum,
            nullable=False,
            server_default="IN_CHAT",
            comment="Context: IN_CHAT or STANDALONE",
        ),
        sa.Column(
            "status",
            uploaded_file_status_enum,
            nullable=False,
            server_default="PENDING_OPENAI_UPLOAD",
            comment="Upload pipeline status",
        ),
        sa.Column(
            "is_deleted_by_user",
            sa.Boolean,
            server_default=sa.text("false"),
            nullable=False,
            comment="Soft-delete flag set by user",
        ),
        sa.Column(
            "deleted_by_user_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When user deleted the file",
        ),
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last time this file was accessed/used",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index("idx_uploaded_files_user_id", "uploaded_files", ["user_id"])
    op.create_index("idx_uploaded_files_content_hash", "uploaded_files", ["content_hash"])
    op.create_index("idx_uploaded_files_status", "uploaded_files", ["status"])

    # ------------------------------------------------------------------ #
    # 3. file_versions — OpenAI file IDs per uploaded file
    # ------------------------------------------------------------------ #
    op.create_table(
        "file_versions",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "uploaded_file_id",
            sa.CHAR(36),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to the uploaded file",
        ),
        sa.Column(
            "openai_file_id",
            sa.String(200),
            nullable=False,
            comment="OpenAI file ID for this version",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
            comment="Whether this file ID is currently active/valid",
        ),
        sa.Column(
            "inactive_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this file version became inactive",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
            comment="Last time we verified this file ID exists in OpenAI",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index("idx_file_versions_uploaded_file_id", "file_versions", ["uploaded_file_id"])
    op.create_index("idx_file_versions_openai_file_id", "file_versions", ["openai_file_id"])
    # Partial unique index: only one active version per uploaded file
    op.create_index(
        "uq_file_versions_active_per_file",
        "file_versions",
        ["uploaded_file_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # ------------------------------------------------------------------ #
    # 4. vector_store_files — junction: vector stores <-> file versions
    # ------------------------------------------------------------------ #
    op.create_table(
        "vector_store_files",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "user_vector_store_id",
            sa.CHAR(36),
            sa.ForeignKey("user_vector_stores.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to user's vector store",
        ),
        sa.Column(
            "file_version_id",
            sa.CHAR(36),
            sa.ForeignKey("file_versions.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to file version (OpenAI file ID)",
        ),
        sa.Column(
            "attached_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When file was attached to vector store",
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean,
            server_default=sa.text("false"),
            nullable=False,
            comment="Soft-delete flag (set when VS is recreated or file re-uploaded)",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this junction record was soft-deleted",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint(
            "user_vector_store_id", "file_version_id", name="uq_vector_store_file_version"
        ),
    )
    op.create_index("idx_vector_store_files_uvs_id", "vector_store_files", ["user_vector_store_id"])
    op.create_index("idx_vector_store_files_fv_id", "vector_store_files", ["file_version_id"])

    # ------------------------------------------------------------------ #
    # 5. chat_files — tracks which files are used in which chats
    # ------------------------------------------------------------------ #
    op.create_table(
        "chat_files",
        sa.Column("id", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "chat_id",
            sa.CHAR(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to chat/message",
        ),
        sa.Column(
            "uploaded_file_id",
            sa.CHAR(36),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
            comment="Reference to uploaded file",
        ),
        sa.Column(
            "file_version_id",
            sa.CHAR(36),
            sa.ForeignKey("file_versions.id", ondelete="SET NULL"),
            nullable=True,
            comment="Reference to specific file version used",
        ),
        sa.Column(
            "usage_type",
            file_usage_type_enum,
            nullable=False,
            comment="NEW_UPLOAD or REFERENCE (reused from previous upload)",
        ),
        sa.Column(
            "upload_context",
            file_upload_context_enum,
            nullable=True,
            comment="Historical context: IN_CHAT or STANDALONE",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index("idx_chat_files_chat_id", "chat_files", ["chat_id"])
    op.create_index("idx_chat_files_uploaded_file_id", "chat_files", ["uploaded_file_id"])


def downgrade() -> None:
    """Drop upload-related tables and enum types in reverse dependency order."""
    op.drop_table("chat_files")
    op.drop_table("vector_store_files")
    op.drop_table("file_versions")
    op.drop_table("uploaded_files")
    op.drop_table("user_vector_stores")

    # Drop enum types after all referencing columns are gone
    op.execute("DROP TYPE IF EXISTS uploaded_file_status_enum")
    op.execute("DROP TYPE IF EXISTS file_upload_context_enum")
    op.execute("DROP TYPE IF EXISTS file_usage_type_enum")
