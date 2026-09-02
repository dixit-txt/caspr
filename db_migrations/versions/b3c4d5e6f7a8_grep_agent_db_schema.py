"""Add grep_agent_2 DB schema: uploaded_file_chunks table and parse-time columns on uploaded_files.

Revision ID: b3c4d5e6f7a8
Revises: d7e2f9a4b6c1
Create Date: 2026-06-16

Additive-only migration — no tables dropped, no enum values removed, no columns dropped.
Adds:
  - 8 new columns on uploaded_files (total_pages, doc_map, sections, llm_summary,
    token_index, bigram_index, is_parsed, parsed_at)
  - New table: uploaded_file_chunks
  - 3 new indexes
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b3c4d5e6f7a8'
down_revision = 'd7e2f9a4b6c1'
branch_labels = None
depends_on = None


def upgrade():
    # -------------------------------------------------------------------------
    # Step 1: Add parse-time columns to uploaded_files
    # -------------------------------------------------------------------------
    op.add_column('uploaded_files', sa.Column(
        'total_pages', sa.Integer(), nullable=True,
        comment='Total pages in the document (set after parsing)'
    ))
    op.add_column('uploaded_files', sa.Column(
        'doc_map', sa.Text(), nullable=True,
        comment='TOC-like structure string for worker system prompt'
    ))
    op.add_column('uploaded_files', sa.Column(
        'sections', postgresql.JSONB(), nullable=True,
        comment='Ordered list of detected heading strings'
    ))
    op.add_column('uploaded_files', sa.Column(
        'llm_summary', sa.Text(), nullable=True,
        comment='60-100 word LLM summary used by coordinator for routing'
    ))
    op.add_column('uploaded_files', sa.Column(
        'token_index', postgresql.JSONB(), nullable=True,
        comment='Inverted index: {token: {chunk_id_str: count}}'
    ))
    op.add_column('uploaded_files', sa.Column(
        'bigram_index', postgresql.JSONB(), nullable=True,
        comment='Bigram index: {bigram: {chunk_id_str: count}}'
    ))
    op.add_column('uploaded_files', sa.Column(
        'is_parsed', sa.Boolean(), nullable=False, server_default='false',
        comment='True once chunks and indexes are stored in DB'
    ))
    op.add_column('uploaded_files', sa.Column(
        'parsed_at', sa.DateTime(timezone=True), nullable=True,
        comment='When parsing completed'
    ))

    # -------------------------------------------------------------------------
    # Step 2: Create uploaded_file_chunks table
    # -------------------------------------------------------------------------
    op.create_table(
        'uploaded_file_chunks',
        sa.Column('id', sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            'uploaded_file_id', sa.CHAR(36),
            sa.ForeignKey('uploaded_files.id', ondelete='CASCADE'),
            nullable=False,
            comment='Reference to uploaded file'
        ),
        sa.Column(
            'chunk_id', sa.Integer(), nullable=False,
            comment='0-based sequential chunk index within this document'
        ),
        sa.Column('text', sa.Text(), nullable=False,
                  comment='~800 char chunk text extracted from the PDF'),
        sa.Column('page_num', sa.Integer(), nullable=False,
                  comment='1-based page number this chunk came from'),
        sa.Column('section', sa.String(500), nullable=True,
                  comment='Most recent detected heading before this chunk'),
        sa.Column('start_offset', sa.Integer(), nullable=False,
                  comment='Character offset from start of full document text'),
        sa.Column('end_offset', sa.Integer(), nullable=False,
                  comment='Character offset end'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('uploaded_file_id', 'chunk_id', name='uq_ufc_file_chunk'),
    )

    # -------------------------------------------------------------------------
    # Step 3: Create indexes
    # -------------------------------------------------------------------------
    op.create_index(
        'idx_uploaded_files_is_parsed',
        'uploaded_files',
        ['is_parsed']
    )
    op.create_index(
        'idx_ufc_uploaded_file_id',
        'uploaded_file_chunks',
        ['uploaded_file_id']
    )
    op.create_index(
        'idx_ufc_file_page',
        'uploaded_file_chunks',
        ['uploaded_file_id', 'page_num']
    )


def downgrade():
    # -------------------------------------------------------------------------
    # Reverse in opposite order
    # -------------------------------------------------------------------------
    op.drop_index('idx_ufc_file_page', table_name='uploaded_file_chunks')
    op.drop_index('idx_ufc_uploaded_file_id', table_name='uploaded_file_chunks')
    op.drop_index('idx_uploaded_files_is_parsed', table_name='uploaded_files')

    op.drop_table('uploaded_file_chunks')

    op.drop_column('uploaded_files', 'parsed_at')
    op.drop_column('uploaded_files', 'is_parsed')
    op.drop_column('uploaded_files', 'bigram_index')
    op.drop_column('uploaded_files', 'token_index')
    op.drop_column('uploaded_files', 'llm_summary')
    op.drop_column('uploaded_files', 'sections')
    op.drop_column('uploaded_files', 'doc_map')
    op.drop_column('uploaded_files', 'total_pages')
