"""internal_db_api.py — the ONLY way grep-service, report-render-service,
and ask-caspr-service touch Postgres.

Per the "centralize all DB access in caspr-api" decision: neither
grep-service, report-render-service, nor ask-caspr-service holds a
database connection or a SQLAlchemy model file anymore. Every read/write
they used to do directly (via src/db/grep_db.py, upload_db.py,
async_db_functions.py, wallet_functions.py, ask_caspr_db.py — all still
present here, unchanged) now happens through one of the endpoints below.

Design principle: endpoints match *use-cases*, not a 1:1 mirror of every
underlying query function. Where several small DB calls used to happen
together inside one `async_session_scope()` on the caller's side (e.g.
"store parse metadata, then bulk-insert chunks"), they're kept together
here as one endpoint so the transaction boundary stays inside caspr-api
instead of being split across multiple HTTP round-trips with no atomicity.

SECURITY: this router has no auth of its own — see caspr-api/README.md
"Known follow-up work". Put it on a private network / behind a
shared-secret header before this is reachable from anywhere but
grep-service, report-render-service, and ask-caspr-service.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.config.log_helper import setup_logging
from src.db.db_utils import async_session_scope
# NOTE: async_session_scope() does NOT auto-commit (see db_utils.py — the
# commit() call inside it is commented out; the monolith's convention is
# "caller commits explicitly after writes", same as api.py/wallet_api.py do
# ~21 times). Every write endpoint below calls `await session.commit()`
# itself for exactly that reason — GET-only endpoints don't need it.
from src.db.enums import FileUploadContext, FileUsageType, UploadedFileStatus
from src.db import grep_db, upload_db
from sqlalchemy import select
from src.db.database import Message
from src.db.async_db_functions import (
    get_user_details, insert_cost_tracker,
    check_user_by_id, get_report_details, update_report,
    get_report_in_cards_format, get_latest_card_version,
)
from src.core.refine.refine_persist import persist_refined_card
from src.db.ask_caspr_db import (
    validate_section_exists_in_ask_caspr,
    get_latest_ask_caspr_chat_entry,
    update_ask_caspr_chat_conversation,
    get_card_data_for_ask_caspr,
    get_report_data_for_ask_caspr,
    get_refinement_history_for_ask_caspr,
    get_ask_caspr_chat_for_version,
)
from src.db.web_search_db import log_web_search_event
from src.core.integrations.openai_file_utils import ensure_report_file_id
from src.db.wallet_functions import get_active_subscription
from src.db.database import UserVectorStore, FileVersion

logger = setup_logging(__file__)

router = APIRouter()


def _row(obj, fields: List[str]) -> Dict[str, Any]:
    """Serialize an ORM row to a plain dict for JSON responses.
    datetimes -> isoformat, enums -> .value, everything else passed through.
    """
    out: Dict[str, Any] = {}
    for f in fields:
        v = getattr(obj, f, None)
        if isinstance(v, dt.datetime):
            v = v.isoformat()
        elif hasattr(v, "value") and not isinstance(v, (dict, list)):
            v = v.value
        out[f] = v
    return out


_UPLOADED_FILE_FIELDS = [
    "id", "user_id", "s3_uri", "original_filename", "file_size", "file_type",
    "content_hash", "upload_context", "status", "is_deleted_by_user",
    "deleted_by_user_at", "last_accessed_at", "created_at", "updated_at",
    "total_pages", "doc_map", "sections", "llm_summary", "token_index",
    "bigram_index", "is_parsed", "parsed_at",
]
_CHUNK_FIELDS = ["id", "uploaded_file_id", "chunk_id", "text", "page_num", "section", "start_offset", "end_offset"]
_VECTOR_STORE_FIELDS = ["id", "user_id", "vector_store_id", "last_accessed_at", "created_at", "updated_at"]
_FILE_VERSION_FIELDS = ["id", "uploaded_file_id", "openai_file_id", "is_active", "inactive_at", "created_at", "last_verified_at"]
_VS_FILE_FIELDS = ["id", "user_vector_store_id", "file_version_id", "created_at", "is_deleted", "deleted_at"]
_CHAT_FILE_FIELDS = ["id", "chat_id", "uploaded_file_id", "file_version_id", "usage_type", "upload_context", "created_at"]


# ============================================================================
# UploadedFile
# ============================================================================

class CreateUploadedFileRequest(BaseModel):
    user_id: str
    s3_uri: str
    original_filename: str
    file_size: int
    file_type: str
    content_hash: str
    upload_context: str = FileUploadContext.IN_CHAT.value
    status: str = UploadedFileStatus.PROCESSING.value


@router.post("/internal/db/grep/uploaded-files")
async def create_uploaded_file(data: CreateUploadedFileRequest):
    async with async_session_scope() as session:
        record = await upload_db.create_uploaded_file(
            user_id=data.user_id, s3_uri=data.s3_uri,
            original_filename=data.original_filename, file_size=data.file_size,
            file_type=data.file_type, content_hash=data.content_hash,
            upload_context=FileUploadContext(data.upload_context),
            status=UploadedFileStatus(data.status), session=session,
        )
        await session.commit()
        return _row(record, _UPLOADED_FILE_FIELDS)


@router.get("/internal/db/grep/uploaded-files/by-hash")
async def get_uploaded_file_by_hash(user_id: str, content_hash: str):
    async with async_session_scope() as session:
        record = await upload_db.get_uploaded_file_by_hash(user_id, content_hash, session)
        return _row(record, _UPLOADED_FILE_FIELDS) if record else None


@router.get("/internal/db/grep/uploaded-files")
async def list_uploaded_files(user_id: str, limit: Optional[int] = None, offset: Optional[int] = None):
    async with async_session_scope() as session:
        result = await upload_db.get_user_uploaded_files(user_id, session, limit=limit, offset=offset)
        return {"files": [_row(f, _UPLOADED_FILE_FIELDS) for f in result["files"]], "total": result["total"]}


@router.get("/internal/db/grep/uploaded-files/{uploaded_file_id}")
async def get_uploaded_file(uploaded_file_id: str):
    async with async_session_scope() as session:
        record = await upload_db.get_uploaded_file_by_id(uploaded_file_id, session)
        if not record:
            raise HTTPException(status_code=404, detail="uploaded_file not found")
        return _row(record, _UPLOADED_FILE_FIELDS)


class PatchUploadedFileRequest(BaseModel):
    status: Optional[str] = None
    touch_accessed: bool = False
    soft_delete: bool = False


@router.patch("/internal/db/grep/uploaded-files/{uploaded_file_id}")
async def patch_uploaded_file(uploaded_file_id: str, data: PatchUploadedFileRequest):
    """Combines update_uploaded_file_status / touch_uploaded_file_accessed /
    soft_delete_uploaded_file — all single-row mutations on the same record,
    frequently called together (e.g. status update + touch)."""
    async with async_session_scope() as session:
        record = await upload_db.get_uploaded_file_by_id(uploaded_file_id, session)
        if not record:
            raise HTTPException(status_code=404, detail="uploaded_file not found")
        if data.status is not None:
            await upload_db.update_uploaded_file_status(record, UploadedFileStatus(data.status), session)
        if data.touch_accessed:
            await upload_db.touch_uploaded_file_accessed(record, session)
        if data.soft_delete:
            await upload_db.soft_delete_uploaded_file(record, session)
        await session.commit()
        return _row(record, _UPLOADED_FILE_FIELDS)


class ChunkIn(BaseModel):
    chunk_id: int
    text: str
    page_num: int
    section: str = ""
    start_offset: int
    end_offset: int


class StoreParseResultRequest(BaseModel):
    total_pages: int
    doc_map: str
    sections: List[str]
    llm_summary: str
    token_index: Dict[str, Dict[str, int]]
    bigram_index: Dict[str, Dict[str, int]]
    total_chunks: int  # matches inv_index.total_chunks — if 0, is_parsed stays False upstream
    chunks: List[ChunkIn]


@router.post("/internal/db/grep/uploaded-files/{uploaded_file_id}/parse-result")
async def store_parse_result(uploaded_file_id: str, data: StoreParseResultRequest):
    """Combines store_parsed_file_metadata + bulk_create_uploaded_file_chunks
    into one transaction — in the old in-process code these always ran back
    to back right after a document finished parsing."""
    async with async_session_scope() as session:
        uploaded_file = await upload_db.get_uploaded_file_by_id(uploaded_file_id, session)
        if not uploaded_file:
            raise HTTPException(status_code=404, detail="uploaded_file not found")

        class _DocIndex:
            total_pages = data.total_pages
            doc_map = data.doc_map
            sections = data.sections

        class _InvIndex:
            total_chunks = data.total_chunks
            token_to_chunks = {
                token: {int(cid): count for cid, count in m.items()}
                for token, m in data.token_index.items()
            }
            bigram_to_chunks = {
                bigram: {int(cid): count for cid, count in m.items()}
                for bigram, m in data.bigram_index.items()
            }

        await grep_db.store_parsed_file_metadata(
            uploaded_file, _DocIndex(), _InvIndex(), data.llm_summary, session,
        )
        if data.total_chunks > 0 and data.chunks:
            class _Chunk:
                def __init__(self, c: ChunkIn):
                    self.chunk_id = c.chunk_id
                    self.text = c.text
                    self.page_num = c.page_num
                    self.section = c.section
                    self.start_offset = c.start_offset
                    self.end_offset = c.end_offset

            await grep_db.bulk_create_uploaded_file_chunks(
                uploaded_file_id, [_Chunk(c) for c in data.chunks], session,
            )
        await session.commit()
        return _row(uploaded_file, _UPLOADED_FILE_FIELDS)


@router.get("/internal/db/grep/uploaded-files/by-ids")
async def get_uploaded_files_by_ids(ids: str = Query(..., description="comma-separated uploaded_file ids")):
    """Unfiltered fetch (unlike session-data below, which only returns
    is_parsed+completed rows) — used by grep-service's chat-reference-files
    lookup, which must show files regardless of parse status."""
    id_list = [i for i in ids.split(",") if i]
    async with async_session_scope() as session:
        from sqlalchemy import select as _select
        from src.db.database import UploadedFile as _UploadedFile
        result = await session.execute(_select(_UploadedFile).where(_UploadedFile.id.in_(id_list)))
        rows = result.scalars().all()
        return {"files": [_row(r, _UPLOADED_FILE_FIELDS) for r in rows]}


@router.get("/internal/db/messages/{chat_id}/owner")
async def get_message_owner(chat_id: str):
    """Used by grep-service's upload endpoints to verify chat ownership
    before attaching files (replaces the raw `select(Message.user_id)...`
    query those endpoints used to run directly)."""
    async with async_session_scope() as session:
        result = await session.execute(select(Message.user_id).where(Message.id == chat_id))
        owner = result.scalar_one_or_none()
        return {"user_id": owner}


@router.get("/internal/db/grep/uploaded-files/session-data")
async def get_session_data(ids: str = Query(..., description="comma-separated uploaded_file ids")):
    """The one latency-sensitive read on this router: everything
    grep_agent_2.create_session_from_db() needs (filtered file rows +
    their chunks) in a single round trip, instead of the N+1 pattern a
    literal 1:1 endpoint mirror would force on the caller."""
    id_list = [i for i in ids.split(",") if i]
    async with async_session_scope() as session:
        files = await grep_db.get_uploaded_files_for_grep(id_list, session)
        out = []
        for f in files:
            chunks = await grep_db.get_uploaded_file_chunks(f.id, session)
            row = _row(f, _UPLOADED_FILE_FIELDS)
            row["chunks"] = [_row(c, _CHUNK_FIELDS) for c in chunks]
            out.append(row)
        return {"files": out}


# ============================================================================
# ChatFile
# ============================================================================

class CreateChatFileRequest(BaseModel):
    chat_id: str
    uploaded_file_id: str
    file_version_id: Optional[str] = None
    usage_type: str
    upload_context: Optional[str] = None


@router.post("/internal/db/grep/chat-files")
async def create_chat_file(data: CreateChatFileRequest):
    async with async_session_scope() as session:
        record = await upload_db.create_chat_file(
            chat_id=data.chat_id, uploaded_file_id=data.uploaded_file_id,
            file_version_id=data.file_version_id,
            usage_type=FileUsageType(data.usage_type),
            upload_context=FileUploadContext(data.upload_context) if data.upload_context else None,
            session=session,
        )
        await session.commit()
        return _row(record, _CHAT_FILE_FIELDS)


@router.get("/internal/db/grep/chat-files")
async def list_chat_files(chat_id: str):
    async with async_session_scope() as session:
        files = await upload_db.get_chat_files(chat_id, session)
        return {
            "files": [_row(f, _CHAT_FILE_FIELDS) for f in files],
            "has_files": len(files) > 0,
            "uploaded_file_ids": [f.uploaded_file_id for f in files],
        }


# ============================================================================
# Vector store / file version / vector-store-file junction
# (OpenAI vector-store bookkeeping — grep-service's ingestion path touches
# these tables when attaching newly-parsed files.)
# ============================================================================

class CreateVectorStoreRequest(BaseModel):
    user_id: str
    vector_store_id: str


@router.get("/internal/db/grep/vector-store")
async def get_vector_store(user_id: str):
    async with async_session_scope() as session:
        record = await upload_db.get_user_vector_store(user_id, session)
        return _row(record, _VECTOR_STORE_FIELDS) if record else None


@router.post("/internal/db/grep/vector-store")
async def create_vector_store(data: CreateVectorStoreRequest):
    async with async_session_scope() as session:
        record = await upload_db.create_user_vector_store(data.user_id, data.vector_store_id, session)
        await session.commit()
        return _row(record, _VECTOR_STORE_FIELDS)


class PatchVectorStoreRequest(BaseModel):
    new_vector_store_id: Optional[str] = None
    touch_accessed: bool = False


@router.patch("/internal/db/grep/vector-store/{vs_id}")
async def patch_vector_store(vs_id: str, data: PatchVectorStoreRequest):
    async with async_session_scope() as session:
        record = await session.get(UserVectorStore, vs_id)
        if not record:
            raise HTTPException(status_code=404, detail="vector_store not found")
        if data.new_vector_store_id is not None:
            await upload_db.update_user_vector_store_id(record, data.new_vector_store_id, session)
        if data.touch_accessed:
            await upload_db.touch_vector_store_accessed(record, session)
        await session.commit()
        return _row(record, _VECTOR_STORE_FIELDS)


class CreateFileVersionRequest(BaseModel):
    uploaded_file_id: str
    openai_file_id: str


@router.post("/internal/db/grep/file-versions")
async def create_file_version(data: CreateFileVersionRequest):
    async with async_session_scope() as session:
        record = await upload_db.create_file_version(
            uploaded_file_id=data.uploaded_file_id, openai_file_id=data.openai_file_id, session=session,
        )
        await session.commit()
        return _row(record, _FILE_VERSION_FIELDS)


@router.get("/internal/db/grep/file-versions/active")
async def get_active_file_version(uploaded_file_id: str):
    async with async_session_scope() as session:
        record = await upload_db.get_active_file_version(uploaded_file_id, session)
        return _row(record, _FILE_VERSION_FIELDS) if record else None


class PatchFileVersionRequest(BaseModel):
    touch_verified: bool = False
    deactivate: bool = False


@router.patch("/internal/db/grep/file-versions/{file_version_id}")
async def patch_file_version(file_version_id: str, data: PatchFileVersionRequest):
    async with async_session_scope() as session:
        record = await session.get(FileVersion, file_version_id)
        if not record:
            raise HTTPException(status_code=404, detail="file_version not found")
        if data.touch_verified:
            await upload_db.touch_file_version_verified(record, session)
        if data.deactivate:
            await upload_db.deactivate_file_version(record, session)
        await session.commit()
        return _row(record, _FILE_VERSION_FIELDS)


class CreateVSFileRequest(BaseModel):
    user_vector_store_id: str
    file_version_id: str


@router.post("/internal/db/grep/vector-store-files")
async def create_vector_store_file(data: CreateVSFileRequest):
    async with async_session_scope() as session:
        record = await upload_db.create_vector_store_file(
            user_vector_store_id=data.user_vector_store_id, file_version_id=data.file_version_id, session=session,
        )
        await session.commit()
        return _row(record, _VS_FILE_FIELDS)


@router.get("/internal/db/grep/vector-store-files")
async def list_vector_store_files(user_vector_store_id: str):
    async with async_session_scope() as session:
        rows = await upload_db.get_active_vector_store_files(user_vector_store_id, session)
        return {"files": [_row(r, _VS_FILE_FIELDS) for r in rows]}


@router.get("/internal/db/grep/vector-store-files/exists")
async def vector_store_file_exists(user_vector_store_id: str, file_version_id: str):
    async with async_session_scope() as session:
        exists = await upload_db.check_file_version_in_vector_store(user_vector_store_id, file_version_id, session)
        return {"exists": exists}


@router.delete("/internal/db/grep/vector-store-files")
async def delete_vector_store_files(user_vector_store_id: str, file_version_id: Optional[str] = None):
    async with async_session_scope() as session:
        if file_version_id:
            deleted = await upload_db.soft_delete_vector_store_file_by_version(user_vector_store_id, file_version_id, session)
            await session.commit()
            return {"deleted_count": 1 if deleted else 0}
        count = await upload_db.soft_delete_vector_store_files_by_vs(user_vector_store_id, session)
        await session.commit()
        return {"deleted_count": count}


# ============================================================================
# Composite: upload_file_config (OpenAI file-search attachment payload)
# ============================================================================

@router.get("/internal/db/grep/upload-file-config/by-chat")
async def upload_file_config_by_chat(chat_id: str, user_id: str):
    async with async_session_scope() as session:
        return await upload_db.build_upload_file_config_from_chat(chat_id, user_id, session)


class UploadFileConfigByRefsRequest(BaseModel):
    reference_ids: List[str]
    user_id: str


@router.post("/internal/db/grep/upload-file-config/by-references")
async def upload_file_config_by_references(data: UploadFileConfigByRefsRequest):
    async with async_session_scope() as session:
        return await upload_db.build_upload_file_config_from_reference_ids(data.reference_ids, data.user_id, session)


# ============================================================================
# Shared: cost tracker, user lookup, wallet/subscription
# ============================================================================

class CostTrackerRequest(BaseModel):
    timestamp: dt.datetime
    model_name: str
    context: Optional[str] = None
    functionality: Optional[str] = None
    agent_name: Optional[str] = None
    chat_id: Optional[str] = None
    user_id: Optional[str] = None
    usage_metadata: Optional[Dict[str, Any]] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None
    cost_details: Optional[Dict[str, Any]] = None


@router.post("/internal/db/cost-tracker")
async def create_cost_tracker_row(data: CostTrackerRequest):
    """Called after every LLM call in grep-service and report-render-service
    (replaces the direct `insert_cost_tracker` call formerly made from
    llm_response_logger.py in both services). Callers already wrap this in
    try/except and treat failure as non-fatal telemetry — same contract
    applies to this endpoint."""
    async with async_session_scope() as session:
        result = await insert_cost_tracker(
            timestamp=data.timestamp, model_name=data.model_name, context=data.context,
            functionality=data.functionality, agent_name=data.agent_name,
            chat_id=data.chat_id, user_id=data.user_id, usage_metadata=data.usage_metadata,
            input_tokens=data.input_tokens, output_tokens=data.output_tokens,
            estimated_cost=data.estimated_cost, cost_details=data.cost_details,
            session=session,
        )
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "insert failed"))
        await session.commit()
        return result


@router.get("/internal/db/users/{user_id}")
async def get_user(user_id: str):
    async with async_session_scope() as session:
        result = await get_user_details(user_id, session)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error", "user not found"))
        return result


@router.get("/internal/db/wallet/subscription")
async def get_subscription(user_id: str):
    async with async_session_scope() as session:
        return await get_active_subscription(session=session, user_id=user_id)


# ============================================================================
# Ask Caspr (ask-caspr-service — Q&A against report sections)
# ============================================================================

class UpdateAskCasprChatRequest(BaseModel):
    chat: List[Dict[str, Any]]


class UpdateReportRequest(BaseModel):
    update_data: Dict[str, Any]


class EnsureReportFileRequest(BaseModel):
    report_id: str
    file_id: Optional[str] = None
    file_s3_path: Optional[str] = None
    log_prefix: str = "ENSURE_REPORT_FILE"


class WebSearchEventRequest(BaseModel):
    trigger_source: str
    user_query: str
    raw_citations: Optional[list] = None
    model_used: Optional[str] = None
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    report_id: Optional[str] = None
    section_name: Optional[str] = None
    candidate_links: Optional[list] = None
    cited_links: Optional[list] = None
    operation_id: Optional[str] = None
    attempt_number: int = 0
    provider: Optional[str] = None
    provider_response_id: Optional[str] = None
    provider_queries: Optional[list] = None
    status: Optional[str] = "succeeded"
    error_type: Optional[str] = None
    duration_ms: Optional[int] = None
    search_call_count: int = 0
    usage_metadata: Optional[Dict[str, Any]] = None
    card_id: Optional[str] = None
    previous_response_id: Optional[str] = None
    response_created_at: Optional[Any] = None
    raw_response: Optional[Dict[str, Any]] = None


@router.get("/internal/db/ask-caspr/users/{user_id}/exists")
async def ask_caspr_check_user(user_id: str):
    async with async_session_scope() as session:
        return await check_user_by_id(user_id, session)


@router.get("/internal/db/ask-caspr/reports/{report_id}")
async def ask_caspr_get_report(report_id: str):
    async with async_session_scope() as session:
        return await get_report_details(report_id, session)


@router.get("/internal/db/ask-caspr/reports/{report_id}/data")
async def ask_caspr_get_report_data(report_id: str):
    async with async_session_scope() as session:
        return await get_report_data_for_ask_caspr(report_id, session)


@router.get("/internal/db/ask-caspr/reports/{report_id}/card-data")
async def ask_caspr_get_card_data(
    report_id: str,
    section_id: str,
    subsection_id: Optional[str] = None,
):
    async with async_session_scope() as session:
        return await get_card_data_for_ask_caspr(
            report_id, section_id, session, subsection_id=subsection_id,
        )


@router.get("/internal/db/ask-caspr/reports/{report_id}/refinement-history")
async def ask_caspr_get_refinement_history(report_id: str):
    async with async_session_scope() as session:
        return await get_refinement_history_for_ask_caspr(report_id, session)


@router.get("/internal/db/ask-caspr/reports/{report_id}/cards-format")
async def ask_caspr_get_report_in_cards_format(report_id: str):
    """Writer input: active cards in the cards_for_db shape refine_card expects."""
    async with async_session_scope() as session:
        return await get_report_in_cards_format(report_id, session)


@router.get("/internal/db/ask-caspr/cards/{card_id}/latest-version")
async def ask_caspr_get_latest_card_version(card_id: str):
    async with async_session_scope() as session:
        return await get_latest_card_version(session, card_id)


@router.patch("/internal/db/ask-caspr/reports/{report_id}")
async def ask_caspr_update_report(report_id: str, data: UpdateReportRequest):
    update_data = dict(data.update_data)
    last_activity = update_data.get("last_activity_at")
    if isinstance(last_activity, str):
        try:
            update_data["last_activity_at"] = dt.datetime.fromisoformat(
                last_activity.replace("Z", "+00:00")
            )
        except ValueError:
            pass
    async with async_session_scope() as session:
        result = await update_report(report_id, update_data, session)
        if result.get("success"):
            await session.commit()
        return result


@router.get("/internal/db/ask-caspr/validate-section")
async def ask_caspr_validate_section(
    report_id: str,
    section_id: str,
    subsection_id: Optional[str] = None,
):
    async with async_session_scope() as session:
        return await validate_section_exists_in_ask_caspr(
            report_id, section_id, session, subsection_id=subsection_id,
        )


@router.get("/internal/db/ask-caspr/chat-entry")
async def ask_caspr_get_latest_chat(
    report_id: str,
    section_id: str,
    subsection_id: Optional[str] = None,
):
    async with async_session_scope() as session:
        return await get_latest_ask_caspr_chat_entry(
            report_id, section_id, session, subsection_id=subsection_id,
        )


@router.patch("/internal/db/ask-caspr/chat-entry/{entry_id}")
async def ask_caspr_update_chat(entry_id: str, data: UpdateAskCasprChatRequest):
    async with async_session_scope() as session:
        # update_ask_caspr_chat_conversation commits internally
        return await update_ask_caspr_chat_conversation(entry_id, data.chat, session)


@router.get("/internal/db/ask-caspr/chat-history")
async def ask_caspr_chat_history(
    report_id: str,
    section_id: str,
    subsection_id: Optional[str] = None,
    report_version_id: Optional[str] = None,
):
    async with async_session_scope() as session:
        return await get_ask_caspr_chat_for_version(
            report_id, section_id, session,
            subsection_id=subsection_id,
            report_version_id=report_version_id,
        )


@router.post("/internal/db/ask-caspr/ensure-report-file")
async def ask_caspr_ensure_report_file(data: EnsureReportFileRequest):
    """OpenAI file-id resolve + persist — kept here so the upload and the
    reports.file_id write stay one use-case on the DB owner."""
    file_id = await ensure_report_file_id(
        report_id=data.report_id,
        file_id=data.file_id,
        file_s3_path=data.file_s3_path,
        log_prefix=data.log_prefix,
    )
    return {"file_id": file_id}


@router.post("/internal/db/ask-caspr/web-search-event")
async def ask_caspr_log_web_search_event(data: WebSearchEventRequest):
    """log_web_search_event opens its own session and swallows errors."""
    await log_web_search_event(**data.model_dump())
    return {"success": True}


class PersistRefineRequest(BaseModel):
    report_id: str
    updated_card: Dict[str, Any]
    user_instruction: str
    refinement_type: str
    table_id_markdown_map: Dict[str, str] = {}
    subsection_id: Optional[str] = None
    updated_refinement_history: List[Dict[str, Any]] = []
    section_id: str
    thread_policy: str
    entry_id: Optional[str] = None
    chat: Optional[List[Dict[str, Any]]] = None


@router.post("/internal/db/ask-caspr/persist-refine")
async def ask_caspr_persist_refine(data: PersistRefineRequest):
    """Composite write: refined card + history + thread policy + REDO_ANALYSIS.

    One session on purpose. ask-caspr-service cannot hold a SQL transaction
    across several HTTP calls. ``thread_policy`` is ``reset_thread`` or
    ``keep_thread``.
    """
    async with async_session_scope() as session:
        result = await persist_refined_card(
            session=session,
            report_id=data.report_id,
            updated_card=data.updated_card,
            user_instruction=data.user_instruction,
            refinement_type=data.refinement_type,
            table_id_markdown_map=data.table_id_markdown_map,
            subsection_id=data.subsection_id,
            updated_refinement_history=data.updated_refinement_history,
            section_id=data.section_id,
            thread_policy=data.thread_policy,
            entry_id=data.entry_id,
            chat=data.chat,
        )
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error") or "persist-refine failed")
        return result
