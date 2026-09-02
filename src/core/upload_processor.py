"""
upload_processor.py
===================
Core business-logic for the file-upload system.

Main public functions:
    ``ensure_vector_store_and_attach_files``
        Reusable function that ensures the user's vector store is valid
        and all reference files are attached.  Handles Cases 1/2/3
        (VS exists, VS deleted from OpenAI, no VS at all).

    ``upload_process_producer``
        Background task that orchestrates the full upload pipeline and
        pushes user-friendly SSE events to a Redis stream.

Internal helpers (trimmed for caspr-api — see note below):
    ``_ensure_vector_store``            – VS creation / recreation (Cases 1-3).
    ``_process_single_reference_file``  – per-file validation + attachment (Case 1 flow).
    ``_upload_attach_and_record``       – reusable: S3 → OpenAI upload + attach + DB records.
    ``compute_file_hash``               – SHA-256 hash for duplicate detection.
    ``validate_upload_file``            – size + extension validation.
    ``process_new_upload_file``         – S3 upload → OpenAI upload → attach to VS.

NOTE: This is a TRIMMED copy for caspr-api. The full original also contains
process_new_upload_file / _parse_reference_file_if_needed /
upload_process_producer (the grep ingestion pipeline) and imports several
src.core.grep_agent_2 submodules directly. Those functions and imports were
dropped here because:
  1. They belong to grep-service's domain now (parsing/chunking/indexing
     uploaded files), and grep-service already has the untrimmed original.
  2. Nothing in caspr-api calls them — only upload_api.py did, and that
     router itself moved to grep-service (see grep-service/README.md).
This file keeps only ensure_vector_store_and_attach_files and its direct
dependencies, which api.py still calls in-process for
the OpenAI-vector-store file-search path (separate from the grep engine).
"""
import asyncio
import hashlib
import json
import os
import tempfile
from uuid_utils import uuid7
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi.concurrency import run_in_threadpool

from src.config.constants import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_FILE_REFERENCES_PER_CHAT,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    S3_REPORTS_BASE_PATH,
)
from src.config.log_helper import setup_logging
from src.core.openai_upload_utils import (
    attach_file_to_vector_store,
    check_file_attached_to_vector_store,
    check_openai_file_exists,
    check_vector_store_exists,
    create_vector_store_for_user,
    upload_file_to_openai,
)
from src.core.openai_file_utils import process_different_file_formats
from src.core.redis_utils import get_redis_instance
from src.core.s3_utils import get_s3_instance, extract_s3_key
from src.db.db_utils import async_session_scope
from src.db.enums import FileUploadContext, FileUsageType, UploadedFileStatus
from src.db.upload_db import (
    check_file_version_in_vector_store,
    create_file_version,
    create_uploaded_file,
    create_user_vector_store,
    create_vector_store_file,
    deactivate_file_version,
    get_active_file_version,
    # get_uploaded_file_by_hash,  # disabled: duplicate detection commented out
    get_uploaded_file_by_id,
    get_user_vector_store,
    soft_delete_vector_store_file_by_version,
    soft_delete_vector_store_files_by_vs,
    touch_file_version_verified,
    touch_uploaded_file_accessed,
    touch_vector_store_accessed,
    update_uploaded_file_status,
    update_user_vector_store_id,
)
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_file_hash(file_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of *file_bytes* for duplicate detection."""
    return hashlib.sha256(file_bytes).hexdigest()


def validate_upload_file(filename: str, file_size: int) -> Optional[str]:
    """
    Validate file size and extension.

    Returns:
        ``None`` if valid, or an error message string.
    """
    # Check extension
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return (
            f"File type '{ext}' is not supported. "
            f"Allowed types: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
        )

    # Check size
    if file_size > MAX_UPLOAD_FILE_SIZE_BYTES:
        max_mb = MAX_UPLOAD_FILE_SIZE_BYTES / (1024 * 1024)
        return f"File size exceeds the maximum limit of {max_mb:.0f} MB."

    return None


def _build_s3_key(user_id: str, user_name: str, upload_session_id: str, filename: str) -> str:
    """
    Build the S3 object key for an uploaded file.

    Pattern:
        ``caspr_reports/YYYY/MM/DD/{user_id}_{user_name}/uploaded_docs/{session_prefix}_{uuid}_{filename}``

    The ``upload_session_id`` is used instead of ``chat_id`` because the upload
    flow is independent of any chat.
    """
    now = datetime.now(timezone.utc)
    date_path = now.strftime("%Y/%m/%d")
    safe_user = (user_name or "unknown").strip().lower().replace(" ", "_")[:60]
    unique_prefix = str(uuid7())[:8]
    safe_filename = filename.replace(" ", "_")
    session_prefix = upload_session_id[:8] if upload_session_id else "no_session"

    return (
        f"{S3_REPORTS_BASE_PATH}/{date_path}/{user_id}_{safe_user}/"
        f"uploaded_docs/{session_prefix}_{unique_prefix}_{safe_filename}"
    )


async def _push_sse(stream_key: str, event: str, data: dict) -> None:
    """Push a single SSE event to a Redis stream."""
    await redis_instance.redis_client.xadd(
        stream_key,
        {"event": event, "data": json.dumps(data)},
    )


# ---------------------------------------------------------------------------
# Reusable internal helpers
# ---------------------------------------------------------------------------

async def _upload_attach_and_record(
    uploaded_file,
    vs_record,
    vector_store_id: str,
    session,
) -> tuple:
    """
    Reusable sub-operation: upload file from S3 to OpenAI, create a new
    active ``file_version``, attach to VS, and create junction row.

    Returns:
        ``(openai_file_id, file_version_record)``

    Raises:
        ``Exception`` if the OpenAI upload or VS attachment fails.
    """
    logger.info(
        f"[_upload_attach_and_record] Starting for uploaded_file_id={uploaded_file.id}, "
        f"vector_store_id={vector_store_id}, s3_uri={uploaded_file.s3_uri}"
    )

    openai_file_id = await run_in_threadpool(upload_file_to_openai, uploaded_file.s3_uri)
    logger.info(
        f"[_upload_attach_and_record] Uploaded to OpenAI: openai_file_id={openai_file_id} "
        f"for uploaded_file_id={uploaded_file.id}"
    )

    fv = await create_file_version(
        uploaded_file_id=uploaded_file.id,
        openai_file_id=openai_file_id,
        session=session,
    )

    attached = await run_in_threadpool(
        attach_file_to_vector_store, openai_file_id, vector_store_id,
        file_name=uploaded_file.original_filename,
    )
    if not attached:
        raise Exception(f"Failed to attach file {openai_file_id} to VS {vector_store_id}")

    await create_vector_store_file(
        user_vector_store_id=vs_record.id,
        file_version_id=fv.id,
        session=session,
    )

    logger.info(
        f"[_upload_attach_and_record] Completed for uploaded_file_id={uploaded_file.id}: "
        f"openai_file_id={openai_file_id}, file_version_id={fv.id}"
    )
    return openai_file_id, fv


async def _process_single_reference_file(
    uploaded_file_id: str,
    user_id: str,
    vs_record,
    vector_store_id: str,
    session,
) -> str:
    """
    Case 1 per-file logic: validate a reference file, ensure it has a
    valid OpenAI file ID, and ensure it is attached to the user's VS.

    Handles all paths:
      A — No active file_version → upload fresh.
      B — openai_file_id dead on OpenAI → deactivate old, re-upload.
      C — Fully attached in both DB and OpenAI → no-op.
      D — Partially attached → clean up stale state, re-attach.

    Returns:
        The ``openai_file_id`` that is confirmed attached to the VS.

    Raises:
        ``ValueError`` if the reference file is invalid (not found / wrong user).
        ``Exception`` on OpenAI or DB failures.
    """
    logger.info(
        f"[_process_single_reference_file] Processing uploaded_file_id={uploaded_file_id}, "
        f"user_id={user_id}, vector_store_id={vector_store_id}"
    )

    # ── Validate uploaded_file exists and belongs to user ──
    uploaded_file = await get_uploaded_file_by_id(uploaded_file_id, session)
    if not uploaded_file:
        logger.warning(f"[_process_single_reference_file] File {uploaded_file_id} not found in DB")
        raise ValueError(f"Reference file {uploaded_file_id} not found")
    if uploaded_file.user_id != user_id:
        logger.warning(
            f"[_process_single_reference_file] File {uploaded_file_id} belongs to "
            f"user_id={uploaded_file.user_id}, not {user_id}"
        )
        raise ValueError(f"Reference file {uploaded_file_id} does not belong to user {user_id}")

    await touch_uploaded_file_accessed(uploaded_file, session)

    # ── Check for active file_version ──
    active_fv = await get_active_file_version(uploaded_file_id, session)

    if not active_fv:
        # Path A: no active version — upload from S3, create everything fresh
        logger.info(f"No active file_version for {uploaded_file_id}, uploading from S3")
        openai_file_id, _ = await _upload_attach_and_record(
            uploaded_file, vs_record, vector_store_id, session,
        )
        return openai_file_id

    openai_file_id = active_fv.openai_file_id
    logger.info(
        f"[_process_single_reference_file] Active file_version found: "
        f"fv_id={active_fv.id}, openai_file_id={openai_file_id}"
    )

    # ── Check if openai_file_id still exists on OpenAI ──
    file_exists = await run_in_threadpool(check_openai_file_exists, openai_file_id)

    if not file_exists:
        # Path B: openai file dead — deactivate old records, re-upload
        logger.warning(f"OpenAI file {openai_file_id} for {uploaded_file_id} not found, re-uploading from S3")
        await deactivate_file_version(active_fv, session)
        await soft_delete_vector_store_file_by_version(vs_record.id, active_fv.id, session)
        openai_file_id, _ = await _upload_attach_and_record(
            uploaded_file, vs_record, vector_store_id, session,
        )
        return openai_file_id

    await touch_file_version_verified(active_fv, session)

    # ── Check attachment in both DB and OpenAI ──
    in_db = await check_file_version_in_vector_store(vs_record.id, active_fv.id, session)
    in_openai = await run_in_threadpool(
        check_file_attached_to_vector_store, openai_file_id, vector_store_id,
    )

    if in_db and in_openai:
        # Path C: fully attached — nothing to do
        logger.info(f"Reference file {uploaded_file_id} already attached to VS, skipping")
        return openai_file_id

    # Path D: not fully attached — clean up stale state and re-attach
    logger.info(f"Reference file {uploaded_file_id} not fully attached (db={in_db}, openai={in_openai}), fixing")

    if in_db:
        await soft_delete_vector_store_file_by_version(vs_record.id, active_fv.id, session)

    if not in_openai:
        attached = await run_in_threadpool(
            attach_file_to_vector_store, openai_file_id, vector_store_id,
            file_name=uploaded_file.original_filename,
        )
        if not attached:
            raise Exception(f"Failed to attach file {openai_file_id} to VS {vector_store_id}")

    await create_vector_store_file(
        user_vector_store_id=vs_record.id,
        file_version_id=active_fv.id,
        session=session,
    )

    logger.info(f"Reference file {uploaded_file_id} attached to VS {vector_store_id}")
    return openai_file_id


# ---------------------------------------------------------------------------
# Vector-store management (Cases 1, 2, 3)
# ---------------------------------------------------------------------------

async def _ensure_vector_store(
    user_id: str,
    user_name: str,
    session,
) -> tuple:
    """
    Ensure the user has a valid OpenAI vector store. Creates or recreates
    as needed.

    Handles:
      Case 1 — VS exists in both DB and OpenAI → return as-is.
      Case 2 — VS in DB but deleted from OpenAI → recreate, bulk soft-delete
               old junction rows, update DB record.
      Case 3 — No VS in DB or OpenAI → create fresh.

    Returns:
        ``(vs_record, vector_store_id)``
    """
    logger.info(f"[_ensure_vector_store] Checking vector store for user_id={user_id}")
    vs_record = await get_user_vector_store(user_id, session)

    # ── Case 3a: no DB record at all ──
    if vs_record is None:
        logger.info(f"No vector store record for user_id={user_id}, creating new one")
        new_vs_id = await run_in_threadpool(create_vector_store_for_user, user_id, user_name)
        vs_record = await create_user_vector_store(user_id, new_vs_id, session)
        return vs_record, new_vs_id

    # ── Case 3b: DB record exists but vs_id is NULL ──
    if not vs_record.vector_store_id:
        logger.info(f"Vector store record has NULL vs_id for user_id={user_id}, creating VS")
        new_vs_id = await run_in_threadpool(create_vector_store_for_user, user_id, user_name)
        await update_user_vector_store_id(vs_record, new_vs_id, session)
        return vs_record, new_vs_id

    # ── vs_id exists in DB — verify it still lives on OpenAI ──
    vs_exists = await run_in_threadpool(check_vector_store_exists, vs_record.vector_store_id)

    if vs_exists:
        # Case 1: exists in both DB and OpenAI
        logger.info(
            f"[_ensure_vector_store] Case 1: VS {vs_record.vector_store_id} exists "
            f"in both DB and OpenAI for user_id={user_id}"
        )
        await touch_vector_store_accessed(vs_record, session)
        return vs_record, vs_record.vector_store_id

    # ── Case 2: in DB but deleted from OpenAI ──
    old_vs_id = vs_record.vector_store_id
    logger.warning(f"Vector store {old_vs_id} deleted on OpenAI for user_id={user_id}, recreating")

    new_vs_id = await run_in_threadpool(create_vector_store_for_user, user_id, user_name)
    await update_user_vector_store_id(vs_record, new_vs_id, session)
    await soft_delete_vector_store_files_by_vs(vs_record.id, session)

    logger.info(f"Recreated vector store for user_id={user_id}: old={old_vs_id} → new={new_vs_id}")
    return vs_record, new_vs_id


# ---------------------------------------------------------------------------
# Main reusable function
# ---------------------------------------------------------------------------

async def ensure_vector_store_and_attach_files(
    user_id: str,
    user_name: str,
    reference_ids: List[str],
    session,
) -> dict:
    """
    Ensure the user's vector store is valid and all reference files are
    uploaded, versioned, and attached.

    This is the single reusable entry point that replaces the old split
    across ``ensure_user_vector_store`` + ``process_reference_file``.

    Args:
        user_id:        The authenticated user's ID.
        user_name:      The user's display name (needed for VS naming).
        reference_ids:  List of ``uploaded_files.id`` to attach.
        session:        The async DB session (caller controls transaction).

    Returns:
        Dict with keys:
        - ``vector_store_id``: str
        - ``file_map``: {uploaded_file_id: openai_file_id}
        - ``file_metadata``: [{"filename": str, "file_type": str}, ...]

    Raises:
        ``ValueError`` if any reference_id is invalid.
        ``Exception`` on unrecoverable OpenAI or DB errors.
    """
    logger.info(
        f"[ensure_vector_store_and_attach_files] Starting for user_id={user_id}, "
        f"reference_ids={reference_ids}"
    )

    vs_record, vector_store_id = await _ensure_vector_store(user_id, user_name, session)
    logger.info(
        f"[ensure_vector_store_and_attach_files] Vector store ready: "
        f"vs_id={vector_store_id} for user_id={user_id}"
    )

    file_map: dict = {}
    file_metadata: List[Dict[str, str]] = []
    
    for ref_id in reference_ids:
        openai_file_id = await _process_single_reference_file(
            ref_id, user_id, vs_record, vector_store_id, session,
        )
        file_map[ref_id] = openai_file_id
        
        # Fetch original filename from uploaded_files
        uploaded_file = await get_uploaded_file_by_id(ref_id, session)
        if uploaded_file:
            file_metadata.append({
                "filename": uploaded_file.original_filename,
                "file_type": uploaded_file.file_type,
            })

    logger.info(
        f"[ensure_vector_store_and_attach_files] Completed for user_id={user_id}: "
        f"vs_id={vector_store_id}, file_map={file_map}, file_metadata={file_metadata}"
    )
    # Sample response:
    # {
    #     "vector_store_id": "vs_abc123",
    #     "file_map": {
    #         "uploaded-file-id-1": "file-openai-id-1",
    #         "uploaded-file-id-2": "file-openai-id-2",
    #     },
    #     "file_metadata": [
    #         {"filename": "document1.pdf", "file_type": "pdf"},
    #         {"filename": "document2.pdf", "file_type": "pdf"},
    #     ]
    # }
    return {
        "vector_store_id": vector_store_id,
        "file_map": file_map,
        "file_metadata": file_metadata,
    }


# ---------------------------------------------------------------------------
# New-upload file processing
# ---------------------------------------------------------------------------

