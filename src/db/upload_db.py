"""
upload_db.py
============
Async database CRUD functions for the file-upload system tables:
  - user_vector_stores
  - uploaded_files
  - file_versions
  - vector_store_files
  - chat_files

All public functions accept an ``AsyncSession`` that the caller obtains via
``async_session_scope()``.
"""

from uuid_utils import uuid7
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.log_helper import setup_logging
from src.db.database import (
    ChatFile,
    FileVersion,
    UploadedFile,
    UserVectorStore,
    VectorStoreFile,
)
from src.db.enums import FileUploadContext, FileUsageType, UploadedFileStatus

logger = setup_logging(__file__)


# ============================================================================
# UserVectorStore
# ============================================================================

async def get_user_vector_store(user_id: str, session: AsyncSession) -> Optional[UserVectorStore]:
    """
    Fetch the single ``UserVectorStore`` row for *user_id*, or ``None``.
    """
    try:
        result = await session.execute(
            select(UserVectorStore).where(UserVectorStore.user_id == user_id)
        )
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error(f"DB error fetching user_vector_store for user_id={user_id}: {e}")
        raise


async def create_user_vector_store(
    user_id: str,
    vector_store_id: str,
    session: AsyncSession,
) -> UserVectorStore:
    """
    Insert a new ``UserVectorStore`` row and flush (but do NOT commit — the
    caller controls the transaction).
    """
    try:
        record = UserVectorStore(
            id=str(uuid7()),
            user_id=user_id,
            vector_store_id=vector_store_id,
        )
        session.add(record)
        await session.flush()
        logger.info(f"Created user_vector_store id={record.id} for user_id={user_id}, vs_id={vector_store_id}")
        return record
    except SQLAlchemyError as e:
        logger.error(f"DB error creating user_vector_store for user_id={user_id}: {e}")
        raise


async def touch_vector_store_accessed(
    vs_record: UserVectorStore,
    session: AsyncSession,
) -> None:
    """
    Bump ``last_accessed_at`` to now — called whenever the vector store is
    used (e.g. during chat or file attachment).  Keeps the cron job from
    reaping actively-used vector stores.
    """
    try:
        vs_record.last_accessed_at = datetime.now(timezone.utc)
        await session.flush()
    except SQLAlchemyError as e:
        logger.error(f"DB error touching user_vector_store last_accessed_at: {e}")
        raise


async def update_user_vector_store_id(
    user_vector_store_record: UserVectorStore,
    new_vector_store_id: str,
    session: AsyncSession,
) -> None:
    """
    Update the ``vector_store_id`` on an existing record (e.g. after VS
    recreation) and bump ``last_accessed_at``.
    """
    try:
        user_vector_store_record.vector_store_id = new_vector_store_id
        user_vector_store_record.last_accessed_at = datetime.now(timezone.utc)
        await session.flush()
        logger.info(
            f"Updated user_vector_store id={user_vector_store_record.id} "
            f"with new vs_id={new_vector_store_id}"
        )
    except SQLAlchemyError as e:
        logger.error(f"DB error updating user_vector_store: {e}")
        raise


# ============================================================================
# UploadedFile
# ============================================================================

async def create_uploaded_file(
    *,
    user_id: str,
    s3_uri: str,
    original_filename: str,
    file_size: int,
    file_type: str,
    content_hash: str,
    upload_context = FileUploadContext.IN_CHAT,
    status = UploadedFileStatus.PROCESSING,
    session: AsyncSession,
) -> UploadedFile:
    """
    Insert a new ``UploadedFile`` row.
    """
    try:
        record = UploadedFile(
            id=str(uuid7()),
            user_id=user_id,
            s3_uri=s3_uri,
            original_filename=original_filename,
            file_size=file_size,
            file_type=file_type,
            content_hash=content_hash,
            upload_context=upload_context,
            status=status,
        )
        session.add(record)
        await session.flush()
        logger.info(
            f"Created uploaded_file id={record.id} for user_id={user_id}, "
            f"filename={original_filename}, status={status}"
        )
        return record
    except SQLAlchemyError as e:
        logger.error(f"DB error creating uploaded_file for user_id={user_id}: {e}")
        raise


async def get_uploaded_file_by_id(
    uploaded_file_id: str,
    session: AsyncSession,
) -> Optional[UploadedFile]:
    """
    Fetch a single ``UploadedFile`` by its primary key.
    """
    try:
        result = await session.execute(
            select(UploadedFile).where(UploadedFile.id == uploaded_file_id)
        )
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error(f"DB error fetching uploaded_file id={uploaded_file_id}: {e}")
        raise


async def get_uploaded_file_by_hash(
    user_id: str,
    content_hash: str,
    session: AsyncSession,
) -> Optional[UploadedFile]:
    """
    Find an existing non-deleted uploaded file for this user with the same
    content hash (duplicate detection).
    """
    try:
        result = await session.execute(
            select(UploadedFile).where(
                UploadedFile.user_id == user_id,
                UploadedFile.content_hash == content_hash,
                UploadedFile.is_deleted_by_user.is_(False),
                UploadedFile.status == UploadedFileStatus.COMPLETED,
            )
        )
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error(f"DB error checking duplicate hash for user_id={user_id}: {e}")
        raise


async def touch_uploaded_file_accessed(
    uploaded_file: UploadedFile,
    session: AsyncSession,
) -> None:
    """
    Bump ``last_accessed_at`` to now — called whenever a file is referenced
    in a chat or upload flow.  Keeps the cron job from reaping actively-used
    uploaded files.
    """
    try:
        uploaded_file.last_accessed_at = datetime.now(timezone.utc)
        await session.flush()
    except SQLAlchemyError as e:
        logger.error(f"DB error touching uploaded_file last_accessed_at: {e}")
        raise


async def update_uploaded_file_status(
    uploaded_file: UploadedFile,
    new_status: UploadedFileStatus,
    session: AsyncSession,
) -> None:
    """
    Update the ``status`` field on an ``UploadedFile``.
    """
    try:
        uploaded_file.status = new_status
        uploaded_file.updated_at = datetime.now(timezone.utc)
        await session.flush()
        logger.info(f"Updated uploaded_file id={uploaded_file.id} status → {new_status}")
    except SQLAlchemyError as e:
        logger.error(f"DB error updating uploaded_file status: {e}")
        raise


async def get_user_uploaded_files(
    user_id: str,
    session: AsyncSession,
    *,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Return non-deleted uploaded files for a user, newest first, with pagination.

    Returns:
        ``{"files": [...], "total": <int>}``
    """
    try:
        base_filter = (
            UploadedFile.user_id == user_id,
            UploadedFile.is_deleted_by_user.is_(False),
        )

        count_result = await session.execute(
            select(func.count()).select_from(UploadedFile).where(*base_filter)
        )
        total = count_result.scalar()

        query = (
            select(UploadedFile)
            .where(*base_filter)
            .order_by(UploadedFile.created_at.desc())
        )
        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        result = await session.execute(query)
        return {"files": list(result.scalars().all()), "total": total}
    except SQLAlchemyError as e:
        logger.error(f"DB error listing uploaded_files for user_id={user_id}: {e}")
        raise


async def soft_delete_uploaded_file(
    uploaded_file: UploadedFile,
    session: AsyncSession,
) -> None:
    """
    Soft-delete an ``UploadedFile`` (set ``is_deleted_by_user=True``).
    """
    try:
        uploaded_file.is_deleted_by_user = True
        uploaded_file.deleted_by_user_at = datetime.now(timezone.utc)
        await session.flush()
        logger.info(f"Soft-deleted uploaded_file id={uploaded_file.id}")
    except SQLAlchemyError as e:
        logger.error(f"DB error soft-deleting uploaded_file: {e}")
        raise


# ============================================================================
# FileVersion
# ============================================================================

async def create_file_version(
    *,
    uploaded_file_id: str,
    openai_file_id: str,
    session: AsyncSession,
) -> FileVersion:
    """
    Insert a new **active** ``FileVersion`` row.
    """
    try:
        record = FileVersion(
            id=str(uuid7()),
            uploaded_file_id=uploaded_file_id,
            openai_file_id=openai_file_id,
            is_active=True,
        )
        session.add(record)
        await session.flush()
        logger.info(
            f"Created file_version id={record.id} for uploaded_file_id={uploaded_file_id}, "
            f"openai_file_id={openai_file_id}"
        )
        return record
    except SQLAlchemyError as e:
        logger.error(f"DB error creating file_version: {e}")
        raise


async def get_active_file_version(
    uploaded_file_id: str,
    session: AsyncSession,
) -> Optional[FileVersion]:
    """
    Get the single *active* ``FileVersion`` for a given uploaded file.
    """
    try:
        result = await session.execute(
            select(FileVersion).where(
                FileVersion.uploaded_file_id == uploaded_file_id,
                FileVersion.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error(f"DB error fetching active file_version for uploaded_file_id={uploaded_file_id}: {e}")
        raise


async def touch_file_version_verified(
    file_version: FileVersion,
    session: AsyncSession,
) -> None:
    """
    Bump ``last_verified_at`` to now — called after confirming the
    OpenAI file ID still exists.  Keeps the cron job from reaping
    actively-used file versions.
    """
    try:
        file_version.last_verified_at = datetime.now(timezone.utc)
        await session.flush()
    except SQLAlchemyError as e:
        logger.error(f"DB error touching file_version last_verified_at: {e}")
        raise


async def deactivate_file_version(
    file_version: FileVersion,
    session: AsyncSession,
) -> None:
    """
    Mark a ``FileVersion`` as inactive (soft-delete the OpenAI file ID mapping).
    """
    try:
        file_version.is_active = False
        file_version.inactive_at = datetime.now(timezone.utc)
        await session.flush()
        logger.info(f"Deactivated file_version id={file_version.id}")
    except SQLAlchemyError as e:
        logger.error(f"DB error deactivating file_version: {e}")
        raise


# ============================================================================
# VectorStoreFile (junction)
# ============================================================================

async def create_vector_store_file(
    *,
    user_vector_store_id: str,
    file_version_id: str,
    session: AsyncSession,
) -> VectorStoreFile:
    """
    Insert a new ``VectorStoreFile`` junction row.
    """
    try:
        record = VectorStoreFile(
            id=str(uuid7()),
            user_vector_store_id=user_vector_store_id,
            file_version_id=file_version_id,
        )
        session.add(record)
        await session.flush()
        logger.info(
            f"Created vector_store_file id={record.id} linking "
            f"vs={user_vector_store_id} ↔ fv={file_version_id}"
        )
        return record
    except SQLAlchemyError as e:
        logger.error(f"DB error creating vector_store_file: {e}")
        raise


async def get_active_vector_store_files(
    user_vector_store_id: str,
    session: AsyncSession,
) -> List[VectorStoreFile]:
    """
    Return all **non-deleted** junction rows for the given vector store record.
    Used during vector store recreation to re-attach previously linked files.
    """
    try:
        result = await session.execute(
            select(VectorStoreFile).where(
                VectorStoreFile.user_vector_store_id == user_vector_store_id,
                VectorStoreFile.is_deleted.is_(False),
            )
        )
        return list(result.scalars().all())
    except SQLAlchemyError as e:
        logger.error(f"DB error listing active vector_store_files for vs={user_vector_store_id}: {e}")
        raise


async def soft_delete_vector_store_files_by_vs(
    user_vector_store_id: str,
    session: AsyncSession,
) -> int:
    """
    Soft-delete **all** junction rows for a given ``user_vector_store_id``.
    Called when the OpenAI vector store is deleted and needs recreation.

    Returns:
        Number of rows updated.
    """
    try:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(VectorStoreFile)
            .where(
                VectorStoreFile.user_vector_store_id == user_vector_store_id,
                VectorStoreFile.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        count = result.rowcount
        await session.flush()
        logger.info(f"Soft-deleted {count} vector_store_files for vs={user_vector_store_id}")
        return count
    except SQLAlchemyError as e:
        logger.error(f"DB error soft-deleting vector_store_files: {e}")
        raise


async def soft_delete_vector_store_file_by_version(
    user_vector_store_id: str,
    file_version_id: str,
    session: AsyncSession,
) -> bool:
    """
    Soft-delete a single junction row matching the given
    ``user_vector_store_id`` + ``file_version_id`` pair.

    Returns:
        ``True`` if a row was soft-deleted, ``False`` if no matching
        non-deleted row was found.
    """
    try:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(VectorStoreFile)
            .where(
                VectorStoreFile.user_vector_store_id == user_vector_store_id,
                VectorStoreFile.file_version_id == file_version_id,
                VectorStoreFile.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        updated = result.rowcount > 0
        if updated:
            await session.flush()
            logger.info(
                f"Soft-deleted vector_store_file for vs={user_vector_store_id}, "
                f"fv={file_version_id}"
            )
        return updated
    except SQLAlchemyError as e:
        logger.error(f"DB error soft-deleting vector_store_file by version: {e}")
        raise


async def check_file_version_in_vector_store(
    user_vector_store_id: str,
    file_version_id: str,
    session: AsyncSession,
) -> bool:
    """
    Check whether a (non-deleted) junction row exists for the given
    ``user_vector_store_id`` + ``file_version_id`` combination.
    """
    try:
        result = await session.execute(
            select(VectorStoreFile.id).where(
                VectorStoreFile.user_vector_store_id == user_vector_store_id,
                VectorStoreFile.file_version_id == file_version_id,
                VectorStoreFile.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none() is not None
    except SQLAlchemyError as e:
        logger.error(f"DB error checking vector_store_file existence: {e}")
        raise


# ============================================================================
# ChatFile
# ============================================================================

async def create_chat_file(
    *,
    chat_id: str,
    uploaded_file_id: str,
    file_version_id: Optional[str],
    usage_type: FileUsageType,
    upload_context: Optional[FileUploadContext] = None,
    session: AsyncSession,
) -> ChatFile:
    """
    Link a file to a chat (``chat_files`` table).

    This must be called **after** the ``Message`` record for the chat has
    been inserted (FK constraint: ``chat_files.chat_id`` → ``messages.id``).
    """
    try:
        record = ChatFile(
            id=str(uuid7()),
            chat_id=chat_id,
            uploaded_file_id=uploaded_file_id,
            file_version_id=file_version_id,
            usage_type=usage_type,
            upload_context=upload_context,
        )
        session.add(record)
        await session.flush()
        logger.info(
            f"Created chat_file id={record.id} linking "
            f"chat={chat_id} ↔ uploaded_file={uploaded_file_id} ({usage_type})"
        )
        return record
    except SQLAlchemyError as e:
        logger.error(f"DB error creating chat_file for chat_id={chat_id}: {e}")
        raise


async def get_chat_files(
    chat_id: str,
    session: AsyncSession,
) -> List[ChatFile]:
    """
    Return all ``ChatFile`` rows for a given chat_id.
    """
    logger.info(f"[DB_GET_CHAT_FILES] Fetching chat files | chat_id={chat_id}")
    try:
        result = await session.execute(
            select(ChatFile).where(ChatFile.chat_id == chat_id)
        )
        files = list(result.scalars().all())
        logger.info(f"[DB_GET_CHAT_FILES] Chat files fetched | chat_id={chat_id} | file_count={len(files)}")
        return files
    except SQLAlchemyError as e:
        logger.error(f"[DB_GET_CHAT_FILES] Database error | chat_id={chat_id} | error={str(e)}", exc_info=True)
        raise


async def chat_has_files(chat_id: str, session: AsyncSession) -> bool:
    """
    Quick check: does the chat already have any associated files?
    """
    try:
        result = await session.execute(
            select(ChatFile.id).where(ChatFile.chat_id == chat_id).limit(1)
        )
        return result.scalar_one_or_none() is not None
    except SQLAlchemyError as e:
        logger.error(f"DB error checking chat_has_files for chat_id={chat_id}: {e}")
        raise


# ============================================================================
# Composite queries used by chat_producer for upload_file_config
# ============================================================================

async def build_upload_file_config_from_chat(
    chat_id: str,
    user_id: str,
    session: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Build the ``upload_file_config`` dict (``{file_ids, vector_store_id, file_metadata}``)
    from existing ``chat_files`` for a continuing chat.

    Returns:
        Dict with keys ``file_ids`` (list of OpenAI file IDs), ``vector_store_id`` (str),
        and ``file_metadata`` (list of dicts with filename info), or ``None`` if the chat has no files.
    """
    try:
        chat_file_rows = await get_chat_files(chat_id, session)
        if not chat_file_rows:
            return None

        # Collect active openai_file_ids and file metadata from file_versions
        openai_file_ids: List[str] = []
        file_metadata: List[Dict[str, str]] = []
        
        for cf in chat_file_rows:
            if cf.file_version_id:
                fv = await session.get(FileVersion, cf.file_version_id)
                if fv and fv.is_active and fv.openai_file_id:
                    openai_file_ids.append(fv.openai_file_id)
                    
                    # Fetch original filename from uploaded_files
                    uploaded_file = await session.get(UploadedFile, cf.uploaded_file_id)
                    if uploaded_file:
                        file_metadata.append({
                            "filename": uploaded_file.original_filename,
                            "file_type": uploaded_file.file_type,
                        })

        if not openai_file_ids:
            logger.warning(f"Chat {chat_id} has chat_files but no active file_versions")
            return None

        # Get user's vector store ID
        vs_record = await get_user_vector_store(user_id, session)
        vector_store_id = vs_record.vector_store_id if vs_record else None

        if not vector_store_id:
            logger.warning(f"Chat {chat_id} has files but user {user_id} has no vector_store_id")
            return None

        return {
            "file_ids": openai_file_ids,
            "vector_store_id": vector_store_id,
            "file_metadata": file_metadata,
        }
    except SQLAlchemyError as e:
        logger.error(f"DB error building upload_file_config for chat_id={chat_id}: {e}")
        raise


async def build_upload_file_config_from_reference_ids(
    reference_ids: List[str],
    user_id: str,
    session: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Build ``upload_file_config`` from a list of ``uploaded_files.id`` values
    (sent by the frontend on the first chat message).

    Returns:
        Dict with keys ``file_ids`` (list of OpenAI file IDs), ``vector_store_id`` (str),
        and ``file_metadata`` (list of dicts with filename info), or ``None``.
    """
    try:
        openai_file_ids: List[str] = []
        file_metadata: List[Dict[str, str]] = []
        
        for ref_id in reference_ids:
            fv = await get_active_file_version(ref_id, session)
            if fv and fv.openai_file_id:
                openai_file_ids.append(fv.openai_file_id)
                
                # Fetch original filename from uploaded_files
                uploaded_file = await session.get(UploadedFile, ref_id)
                if uploaded_file:
                    file_metadata.append({
                        "filename": uploaded_file.original_filename,
                        "file_type": uploaded_file.file_type,
                    })
            else:
                logger.warning(f"No active file_version for uploaded_file_id={ref_id}")

        if not openai_file_ids:
            return None

        vs_record = await get_user_vector_store(user_id, session)
        vector_store_id = vs_record.vector_store_id if vs_record else None

        if not vector_store_id:
            logger.warning(f"User {user_id} has no vector_store_id")
            return None

        return {
            "file_ids": openai_file_ids,
            "vector_store_id": vector_store_id,
            "file_metadata": file_metadata,
        }
    except SQLAlchemyError as e:
        logger.error(f"DB error building upload_file_config from reference_ids: {e}")
        raise

