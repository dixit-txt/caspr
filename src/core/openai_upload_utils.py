"""
openai_upload_utils.py
======================
Synchronous OpenAI file and vector-store operations used by the
file-upload system (``upload_processor.py``).

All functions are **synchronous** because they are called inside
``run_in_threadpool`` from the async upload processor.  They reuse the
same ``SYNC_OPENAI_CLIENT`` instance used elsewhere in the codebase
(e.g. ``openai_file_utils.py``).

Functions:
    1. check_openai_file_exists        – verify a file ID still exists on OpenAI
    2. check_file_attached_to_vector_store – verify a file is attached to a VS
    3. check_vector_store_exists        – verify a vector store ID still exists
    4. upload_file_to_openai            – download from S3, convert, upload
    5. create_vector_store_for_user     – create an empty VS on OpenAI
    6. attach_file_to_vector_store      – attach a file to a VS (with polling)
"""

import os
import time
from uuid_utils import uuid7

from src.config.constants import SYNC_OPENAI_CLIENT
from src.config.log_helper import setup_logging
from src.core.s3_utils import download_file_from_s3_to_local
from src.core.openai_file_utils import process_different_file_formats

logger = setup_logging(__file__)

# Retry configuration shared across functions
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 2
# Timeout for vector store file ingestion polling (seconds)
_VS_INGESTION_TIMEOUT_SECONDS = 600
_VS_INGESTION_POLL_INTERVAL_SECONDS = 2


# ---------------------------------------------------------------------------
# 1. Check whether an OpenAI file still exists
# ---------------------------------------------------------------------------
def check_openai_file_exists(openai_file_id: str) -> bool:
    """
    Verify that a file with the given ID still exists on OpenAI's servers.

    Uses ``SYNC_OPENAI_CLIENT.files.retrieve()`` — if it succeeds the file
    exists; any exception (typically 404) means it does not.

    Args:
        openai_file_id: The OpenAI file ID to check (e.g. ``"file-abc123"``).

    Returns:
        ``True`` if the file exists, ``False`` otherwise.
    """
    try:
        logger.info(f"[check_openai_file_exists] Checking file_id={openai_file_id}")
        SYNC_OPENAI_CLIENT.files.retrieve(file_id=openai_file_id)
        logger.info(f"[check_openai_file_exists] File {openai_file_id} exists on OpenAI")
        return True
    except Exception as e:
        logger.info(
            f"[check_openai_file_exists] File {openai_file_id} does not exist "
            f"on OpenAI (reason: {e})"
        )
        return False


# ---------------------------------------------------------------------------
# 2. Check whether a file is already attached to a vector store
# ---------------------------------------------------------------------------
def check_file_attached_to_vector_store(openai_file_id: str, vector_store_id: str) -> bool:
    """
    Check whether *openai_file_id* is currently attached to *vector_store_id*.

    Uses ``SYNC_OPENAI_CLIENT.vector_stores.files.retrieve()`` which returns
    the attachment record if it exists, or raises an exception if not.

    Args:
        openai_file_id:   The OpenAI file ID.
        vector_store_id:  The OpenAI vector store ID.

    Returns:
        ``True`` if the file is attached, ``False`` otherwise.
    """
    try:
        logger.info(
            f"[check_file_attached_to_vector_store] Checking file={openai_file_id} "
            f"in vector_store={vector_store_id}"
        )
        SYNC_OPENAI_CLIENT.vector_stores.files.retrieve(
            vector_store_id=vector_store_id,
            file_id=openai_file_id,
        )
        logger.info(
            f"[check_file_attached_to_vector_store] File {openai_file_id} IS "
            f"attached to vector_store {vector_store_id}"
        )
        return True
    except Exception as e:
        logger.info(
            f"[check_file_attached_to_vector_store] File {openai_file_id} is "
            f"NOT attached to vector_store {vector_store_id} (reason: {e})"
        )
        return False


# ---------------------------------------------------------------------------
# 3. Check whether a vector store still exists on OpenAI
# ---------------------------------------------------------------------------
def check_vector_store_exists(vector_store_id: str) -> bool:
    """
    Verify that a vector store with the given ID still exists on OpenAI.

    Uses ``SYNC_OPENAI_CLIENT.vector_stores.retrieve()`` — success means it
    exists; any exception (typically 404) means it was deleted or expired.

    Args:
        vector_store_id: The OpenAI vector store ID to check.

    Returns:
        ``True`` if the vector store exists, ``False`` otherwise.
    """
    try:
        logger.info(f"[check_vector_store_exists] Checking vector_store_id={vector_store_id}")
        SYNC_OPENAI_CLIENT.vector_stores.retrieve(vector_store_id=vector_store_id)
        logger.info(f"[check_vector_store_exists] Vector store {vector_store_id} exists on OpenAI")
        return True
    except Exception as e:
        logger.info(
            f"[check_vector_store_exists] Vector store {vector_store_id} does "
            f"not exist on OpenAI (reason: {e})"
        )
        return False


# ---------------------------------------------------------------------------
# 4. Upload a file from S3 to OpenAI
# ---------------------------------------------------------------------------
def upload_file_to_openai(s3_path: str) -> str:
    """
    Download a file from S3, convert it to PDF (if needed), and upload it
    to OpenAI via the Files API.

    Steps:
      1. Download from S3 to a temporary local path.
      2. Convert to PDF using ``process_different_file_formats`` (passthrough
         if already PDF).
      3. Upload the processed file to OpenAI with ``purpose="assistants"``.
      4. Clean up all temporary files.

    Includes retry logic (up to ``_MAX_RETRIES`` attempts) on the OpenAI
    upload step.

    Args:
        s3_path: Full S3 URI, e.g. ``"s3://bucket/path/to/file.pdf"``.

    Returns:
        The new ``openai_file_id`` (string, e.g. ``"file-abc123"``).

    Raises:
        Exception: If the file cannot be downloaded, converted, or uploaded
                   after all retries.
    """
    logger.info(f"[upload_file_to_openai] Starting for s3_path={s3_path}")

    local_file_path = None
    processed_file_path = None

    try:
        # ── Step 1: Download from S3 to local temp ──
        # Extract original filename from S3 path for the temp file
        s3_key_part = s3_path.replace("s3://", "").split("/", 1)[1] if "s3://" in s3_path else s3_path
        original_filename = os.path.basename(s3_key_part)
        local_file_path = f"tmp_upload_{uuid7()}_{original_filename}"

        download_file_from_s3_to_local(s3_path, local_file_path)
        logger.info(f"[upload_file_to_openai] Downloaded from S3 to {local_file_path}")

        # ── Step 2: Convert to PDF (passthrough if already .pdf) ──
        processed_file_path = process_different_file_formats(local_file_path)
        if processed_file_path is None:
            raise Exception(f"File conversion failed for {original_filename}")
        logger.info(f"[upload_file_to_openai] Processed file: {processed_file_path}")

        # ── Step 3: Upload to OpenAI with retries ──
        for attempt in range(_MAX_RETRIES):
            try:
                with open(processed_file_path, "rb") as f:
                    file_obj = SYNC_OPENAI_CLIENT.files.create(
                        file=f,
                        purpose="assistants",
                    )
                openai_file_id = file_obj.id
                logger.info(
                    f"[upload_file_to_openai] Successfully uploaded to OpenAI: "
                    f"openai_file_id={openai_file_id}"
                )
                return openai_file_id

            except Exception as e:
                if attempt == _MAX_RETRIES - 1:
                    logger.error(
                        f"[upload_file_to_openai] Failed after {_MAX_RETRIES} "
                        f"attempts: {e}"
                    )
                    raise Exception(
                        f"Error uploading file to OpenAI after {_MAX_RETRIES} "
                        f"attempts: {e}"
                    )
                logger.warning(
                    f"[upload_file_to_openai] Attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {_RETRY_DELAY_SECONDS}s..."
                )
                time.sleep(_RETRY_DELAY_SECONDS)

    finally:
        # ── Step 4: Clean up temporary files ──
        for path in (local_file_path, processed_file_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    logger.info(f"[upload_file_to_openai] Cleaned up temp file: {path}")
                except Exception as cleanup_err:
                    logger.warning(
                        f"[upload_file_to_openai] Failed to clean up {path}: {cleanup_err}"
                    )


# ---------------------------------------------------------------------------
# 5. Create an empty vector store for a user
# ---------------------------------------------------------------------------
def create_vector_store_for_user(user_id: str, user_name: str) -> str:
    """
    Create a new **empty** vector store on OpenAI for the given user.

    The vector store is named ``caspr_{user_id}_{user_name}`` and includes
    ``user_id`` in its metadata for traceability.

    **Note:** This function does NOT check whether a vector store already
    exists in the database — that responsibility belongs to the caller
    (``ensure_user_vector_store`` in ``upload_processor.py``).  This
    function only creates a new VS on the OpenAI side.

    Args:
        user_id:   The internal user UUID.
        user_name: The user's display name (used in the VS name).

    Returns:
        The newly created ``vector_store_id`` (string).

    Raises:
        Exception: If the OpenAI API call fails.
    """
    # Build a deterministic, human-readable VS name
    safe_user_name = (user_name or "unknown").replace(" ", "_")[:50]
    vector_store_name = f"caspr_{user_id}_{safe_user_name}"

    logger.info(
        f"[create_vector_store_for_user] Creating vector store: "
        f"name={vector_store_name}, user_id={user_id}"
    )

    try:
        vector_store = SYNC_OPENAI_CLIENT.vector_stores.create(
            name=vector_store_name,
            metadata={"user_id": user_id, "user_name": user_name or "unknown"},
        )
        vector_store_id = vector_store.id
        logger.info(
            f"[create_vector_store_for_user] Vector store created: "
            f"vector_store_id={vector_store_id}, name={vector_store_name}"
        )
        return vector_store_id

    except Exception as e:
        logger.error(
            f"[create_vector_store_for_user] Failed to create vector store "
            f"for user_id={user_id}: {e}",
            exc_info=True,
        )
        raise Exception(f"Failed to create vector store on OpenAI: {e}")


# ---------------------------------------------------------------------------
# 6. Attach an OpenAI file to a vector store (with ingestion polling)
# ---------------------------------------------------------------------------
def attach_file_to_vector_store(
    openai_file_id: str,
    vector_store_id: str,
    file_name: str = "",
) -> bool:
    """
    Attach *openai_file_id* to *vector_store_id* on OpenAI.

    Before attaching, verifies that both the file and the vector store
    exist.  After the attach call, polls the ingestion status until it
    reaches ``"completed"`` or ``"failed"`` (or times out).

    Args:
        openai_file_id:  The OpenAI file ID to attach.
        vector_store_id: The target OpenAI vector store ID.
        file_name:       Original filename (stored as searchable attribute).

    Returns:
        ``True`` if the file was attached and ingestion completed
        successfully, ``False`` otherwise.
    """
    logger.info(
        f"[attach_file_to_vector_store] Attaching file={openai_file_id} "
        f"to vector_store={vector_store_id}"
    )

    try:
        # ── Pre-checks ──
        if not check_openai_file_exists(openai_file_id):
            logger.error(
                f"[attach_file_to_vector_store] File {openai_file_id} does not "
                f"exist on OpenAI — cannot attach"
            )
            return False

        if not check_vector_store_exists(vector_store_id):
            logger.error(
                f"[attach_file_to_vector_store] Vector store {vector_store_id} "
                f"does not exist on OpenAI — cannot attach"
            )
            return False

        # ── Attach the file ──
        metadata = {
            "file_id": openai_file_id,
            "file_name": file_name,
            "vector_store_id": vector_store_id,
        }
        file_obj = SYNC_OPENAI_CLIENT.vector_stores.files.create(
            vector_store_id=vector_store_id,
            file_id=openai_file_id,
            attributes=metadata,
            timeout=300,
        )
        logger.info(
            f"[attach_file_to_vector_store] Attach request sent. "
            f"Polling ingestion status..."
        )

        # ── Poll ingestion status until complete, failed, or timeout ──
        poll_start = time.time()
        while True:
            status = SYNC_OPENAI_CLIENT.vector_stores.files.retrieve(
                vector_store_id=vector_store_id,
                file_id=file_obj.id,
            ).status

            logger.info(
                f"[attach_file_to_vector_store] Ingestion status for "
                f"file={openai_file_id}: {status}"
            )

            if status == "completed":
                logger.info(
                    f"[attach_file_to_vector_store] File {openai_file_id} "
                    f"successfully ingested into vector_store {vector_store_id}"
                )
                return True

            if status == "failed":
                logger.error(
                    f"[attach_file_to_vector_store] Ingestion FAILED for "
                    f"file={openai_file_id} in vector_store={vector_store_id}"
                )
                return False

            # Check timeout
            elapsed = time.time() - poll_start
            if elapsed > _VS_INGESTION_TIMEOUT_SECONDS:
                logger.error(
                    f"[attach_file_to_vector_store] Ingestion TIMED OUT after "
                    f"{_VS_INGESTION_TIMEOUT_SECONDS}s for file={openai_file_id}"
                )
                return False

            time.sleep(_VS_INGESTION_POLL_INTERVAL_SECONDS)

    except Exception as e:
        logger.error(
            f"[attach_file_to_vector_store] Error attaching file={openai_file_id} "
            f"to vector_store={vector_store_id}: {e}",
            exc_info=True,
        )
        return False
