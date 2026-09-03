"""grep_agent_2 — HTTP shim (NOT the real engine).

The real grep_agent_2 (coordinator/worker/parallel PDF search) lives in
grep-service now. This package exists only so that code copied unchanged
from the monolith — card_utils.py's
`from app.adapters.grep_agent_2 import ask_pdfs as grep_ask_pdfs, PdfSession` —
keeps working without modification. It reproduces the same public call
signatures (`get_or_create_grep_session`, `ask_pdfs`, `PdfSession`) but
implements them as a network call to grep-service's
`POST /internal/grep/query` instead of an in-process search.

See report-render-service/README.md → "grep_agent_2 shim" for why this
exists and what it does NOT reproduce (PdfSession here is a thin handle,
not the real in-memory doc-index object — nothing in this service reads
its internal fields beyond `.labels`, verified against every copied call
site).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import GREP_SERVICE_BASE_URL
from app.core.logging import setup_logging

logger = setup_logging(__name__)


@dataclass
class _WorkerLabelStub:
    """Stand-in for grep_agent_2.WorkerSession — only `.labels` is read by
    copied call sites (model.py's system-prompt file-naming logic)."""
    labels: List[str]


@dataclass
class PdfSession:
    uploaded_file_ids: List[str]
    labels: List[str] = field(default_factory=list)
    worker_sessions: List[_WorkerLabelStub] = field(default_factory=list)


async def get_or_create_grep_session(
    uploaded_file_ids: List[str],
    db_session: AsyncSession,
) -> Optional["PdfSession"]:
    """Resolve filenames for the given ids so callers can log/display them,
    then hand back a lightweight handle. The real parsing/indexing lookup
    happens inside grep-service when `ask_pdfs()` is actually called — this
    function no longer touches Postgres for chunk data, only for filenames.
    """
    if not uploaded_file_ids:
        return None

    from src.db.database import UploadedFile
    from sqlalchemy import select

    result = await db_session.execute(
        select(UploadedFile.id, UploadedFile.original_filename).where(
            UploadedFile.id.in_(uploaded_file_ids)
        )
    )
    rows = result.all()
    if not rows:
        return None

    ids = [r[0] for r in rows]
    labels = [r[1] or r[0] for r in rows]
    return PdfSession(
        uploaded_file_ids=ids,
        labels=labels,
        worker_sessions=[_WorkerLabelStub(labels=labels)],
    )


def ask_pdfs(
    session: "PdfSession",
    question: str,
    max_iterations_per_worker: int = 4,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronous by design — matches the original grep_agent_2.ask_pdfs
    signature exactly so every call site (card_utils.py, model.py,
    ask_caspr.py) needs zero changes. Blocks the calling thread on an HTTP
    call to grep-service; callers already running inside async code should
    wrap this in `asyncio.to_thread(...)` (see grep-service/main.py's own
    endpoint for the equivalent pattern) if they need the event loop free
    during the call.
    """
    try:
        resp = httpx.post(
            f"{GREP_SERVICE_BASE_URL}/internal/grep/query",
            json={
                "uploaded_file_ids": session.uploaded_file_ids,
                "question": question,
                "max_iterations_per_worker": max_iterations_per_worker,
                "chat_id": chat_id,
                "user_id": user_id,
            },
            timeout=httpx.Timeout(180.0),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"[GREP_CLIENT] ask_pdfs call to grep-service failed: {e}", exc_info=True)
        return {
            "answer": f"Error processing question: {e}",
            "total_tool_calls": 0,
            "all_keywords_used": [],
            "workers_used": 0,
            "workers_total": len(session.worker_sessions) if session else 0,
            "routing_selected": [],
        }
