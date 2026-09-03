"""
grep_db.py
==========
Async database CRUD functions for the grep_agent_2 workflow.

Tables:
  - uploaded_file_chunks  (chunk storage per parsed PDF)
  - uploaded_files        (parse-time metadata columns: is_parsed, token_index, etc.)
  - chat_files            (read-only: fetch file IDs for a chat)

All public functions accept an AsyncSession obtained via async_session_scope().
Pattern is identical to upload_db.py: uuid7() for IDs, SQLAlchemyError handling,
session.flush() (never commit — caller controls the transaction).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from uuid_utils import uuid7
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.log_helper import setup_logging
from src.db.database import ChatFile, UploadedFile, UploadedFileChunk
from src.db.enums import UploadedFileStatus

logger = setup_logging(__file__)


# ============================================================================
# UploadedFileChunk
# ============================================================================

async def bulk_create_uploaded_file_chunks(
    uploaded_file_id: str,
    chunks: list,           # List[Chunk] from grep_agent_2.pdf_parser
    session: AsyncSession,
) -> None:
    """
    Bulk-insert all Chunk objects for a parsed document.

    Each Chunk becomes one UploadedFileChunk row. Uses session.add_all() for
    efficiency instead of individual inserts.
    """
    try:
        rows = [
            UploadedFileChunk(
                id=str(uuid7()),
                uploaded_file_id=uploaded_file_id,
                chunk_id=c.chunk_id,
                text=c.text,
                page_num=c.page_num,
                section=c.section or "",
                start_offset=c.start_offset,
                end_offset=c.end_offset,
            )
            for c in chunks
        ]
        session.add_all(rows)
        await session.flush()
        logger.info(
            f"[grep_db] Inserted {len(rows)} chunks for uploaded_file_id={uploaded_file_id}"
        )
    except SQLAlchemyError as e:
        logger.error(
            f"[grep_db] DB error bulk-inserting chunks for uploaded_file_id={uploaded_file_id}: {e}"
        )
        raise


async def get_uploaded_file_chunks(
    uploaded_file_id: str,
    session: AsyncSession,
) -> List[UploadedFileChunk]:
    """
    Return all chunks for a file, ordered by chunk_id ascending.
    Used when rebuilding a PdfSession from DB.
    """
    try:
        result = await session.execute(
            select(UploadedFileChunk)
            .where(UploadedFileChunk.uploaded_file_id == uploaded_file_id)
            .order_by(UploadedFileChunk.chunk_id)
        )
        return list(result.scalars().all())
    except SQLAlchemyError as e:
        logger.error(
            f"[grep_db] DB error fetching chunks for uploaded_file_id={uploaded_file_id}: {e}"
        )
        raise


# ============================================================================
# UploadedFile — parse-time metadata
# ============================================================================

async def store_parsed_file_metadata(
    uploaded_file: UploadedFile,
    doc_index: Any,         # DocumentIndex from grep_agent_2.pdf_parser
    inv_index: Any,         # InvertedIndex from grep_agent_2.inverted_index
    llm_summary: str,
    session: AsyncSession,
) -> None:
    """
    Write all parse-time columns onto an existing UploadedFile row.

    token_index and bigram_index store int chunk_id keys as strings
    (JSON only supports string keys) — load_from_dict() converts them back.

    Sets is_parsed=True and parsed_at=now() to signal the file is ready
    for grep session reconstruction.

    If the parsed document produced zero chunks (e.g. empty file or blank PDF),
    is_parsed stays False — the file was technically parsed but contains no
    searchable content and should not appear in grep sessions.
    """
    if inv_index.total_chunks == 0:
        logger.warning(
            f"[grep_db] No chunks extracted for uploaded_file_id={uploaded_file.id} "
            f"(filename={uploaded_file.original_filename}). "
            f"is_parsed stays False — file has no searchable content."
        )
        return

    try:
        uploaded_file.total_pages  = doc_index.total_pages
        uploaded_file.doc_map      = doc_index.doc_map
        uploaded_file.sections     = doc_index.sections
        uploaded_file.llm_summary  = llm_summary
        # Convert int chunk_id keys → str for JSONB compatibility
        uploaded_file.token_index  = {
            token: {str(cid): count for cid, count in chunk_map.items()}
            for token, chunk_map in inv_index.token_to_chunks.items()
        }
        uploaded_file.bigram_index = {
            bigram: {str(cid): count for cid, count in chunk_map.items()}
            for bigram, chunk_map in inv_index.bigram_to_chunks.items()
        }
        uploaded_file.is_parsed    = True
        uploaded_file.parsed_at    = datetime.now(timezone.utc)
        await session.flush()
        logger.info(
            f"[grep_db] Stored parse metadata for uploaded_file_id={uploaded_file.id} | "
            f"pages={doc_index.total_pages} | chunks={inv_index.total_chunks} | "
            f"tokens={len(inv_index.token_to_chunks)} | bigrams={len(inv_index.bigram_to_chunks)}"
        )
    except SQLAlchemyError as e:
        logger.error(
            f"[grep_db] DB error storing parse metadata for uploaded_file_id={uploaded_file.id}: {e}"
        )
        raise


async def get_uploaded_files_for_grep(
    uploaded_file_ids: List[str],
    session: AsyncSession,
) -> List[UploadedFile]:
    """
    Fetch UploadedFile rows that are ready for grep session reconstruction.

    Only returns rows where:
      - id IN uploaded_file_ids
      - is_parsed = True
      - status = COMPLETED
      - is_deleted_by_user = False

    Results are returned in the same order as uploaded_file_ids.
    """
    if not uploaded_file_ids:
        return []
    try:
        result = await session.execute(
            select(UploadedFile).where(
                UploadedFile.id.in_(uploaded_file_ids),
                UploadedFile.is_parsed.is_(True),
                UploadedFile.status == UploadedFileStatus.COMPLETED,
                UploadedFile.is_deleted_by_user.is_(False),
            )
        )
        rows = result.scalars().all()
        # Preserve caller's ordering
        id_order = {fid: idx for idx, fid in enumerate(uploaded_file_ids)}
        return sorted(rows, key=lambda r: id_order.get(r.id, 9999))
    except SQLAlchemyError as e:
        logger.error(
            f"[grep_db] DB error fetching uploaded files for grep ids={uploaded_file_ids}: {e}"
        )
        raise


# ============================================================================
# ChatFile — read-only helpers for grep session building
# ============================================================================

async def get_chat_uploaded_file_ids(
    chat_id: str,
    session: AsyncSession,
) -> List[str]:
    """
    Return the list of uploaded_file_ids associated with a chat.
    Used in chat_producer / ask_caspr_producer to build a grep session.
    """
    try:
        result = await session.execute(
            select(ChatFile.uploaded_file_id).where(ChatFile.chat_id == chat_id)
        )
        return list(result.scalars().all())
    except SQLAlchemyError as e:
        logger.error(
            f"[grep_db] DB error fetching chat file ids for chat_id={chat_id}: {e}"
        )
        raise
