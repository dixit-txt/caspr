"""Async HTTP client that drives the caspr backend the same way the frontend does.

The caspr chat is a producer/consumer flow:

1. ``POST /create-session``            -> {session_id, chat_id}
2. ``GET  /chat-stream/{session_id}``  -> SSE stream of events (deltas, cards, ...)
3. ``POST /chat``                      -> starts ``chat_producer`` in the background

This client opens the SSE stream first, fires the ``/chat`` request, then
aggregates the stream into a single result so it can be returned from an MCP
tool call. It never imports ``Casper`` — all auth, wallet, DB and S3 side
effects stay inside the backend and are reused for free.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from src.config.log_helper import setup_logging

logger = setup_logging(__name__)

# SSE ``data`` payload ``type`` values that mean the backend is building a report
# (as opposed to a plain chat reply). Seeing any of these flips the turn into
# "report mode" so we wait for the report-completion marker.
_REPORT_SIGNAL_TYPES = {
    "card",
    "report_layout",
    "report_citations",
    "report_type",
    "tool_call",
}

# Terminal ``type`` markers from ``chat_producer``.
# ``saving_checkpoint_data`` is emitted for every successful turn (chat or report)
# right before DB persistence — use it to end plain chat turns. Report turns
# continue until ``saving_checkpoint_data_done``.
_CHAT_DONE_TYPE = "saving_checkpoint_data"
_REPORT_DONE_TYPE = "saving_checkpoint_data_done"
# Emitted (again) by ``refresh_session`` after a turn's DB save completes.
# Unreliable as a sole end signal (stream key is deleted/recreated), so only used
# as a secondary hint for chat turns.
_SESSION_CREATED_TYPE = "session_created"

# Short timeout for plain REST calls only (create-session, download URL, POST /chat).
# SSE / report streams have no idle or hard deadline — reports can run 1h+.
_REST_TIMEOUT = httpx.Timeout(30.0)
_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)

# Reuse session_id per chat_id across MCP tool turns. Calling create-session on
# every turn overwrites Redis chat_session:{chat_id} and invalidates an in-flight
# SSE connection (UI or a prior MCP stream).
_SESSION_BY_CHAT: Dict[str, str] = {}
_SESSION_LOCK = asyncio.Lock()

# Background report turns started via start_report_turn (Claude Desktop ~60s tool limit).
_ACTIVE_TURNS: Dict[str, Dict[str, Any]] = {}

# Report DB statuses that mean card generation finished successfully.
_REPORT_DONE_STATUSES = frozenset(
    {
        "analysis-completed",
        "generating-output",
        "output-generated",
    }
)

# Host callback: pipeline percent (0–100) + human-readable status + optional SSE type.
ProgressFn = Callable[[float, str, str], Awaitable[None]]


@dataclass
class _TurnProgress:
    """Coalesce SSE events into deduplicated progress updates."""

    progress_pct: float = 0.0
    last_message: str = ""
    card_count: int = 0
    chat_stream_started: bool = False

    async def notify(
        self,
        on_progress: Optional[ProgressFn],
        message: str,
        *,
        percent: float,
        force: bool = False,
        etype: str = "",
    ) -> None:
        if not on_progress or not message:
            return
        if not force and message == self.last_message:
            return
        self.progress_pct = max(self.progress_pct, min(100.0, percent))
        self.last_message = message
        await on_progress(self.progress_pct, message, etype)

    async def from_sse(
        self,
        on_progress: Optional[ProgressFn],
        etype: str,
        data: Dict[str, Any],
    ) -> None:
        message = _sse_progress_message(etype, data, self)
        if not message:
            return
        percent = _sse_progress_percent(etype, data, self)
        await self.notify(
            on_progress,
            message,
            percent=percent,
            force=etype == "card_generation_heartbeat",
            etype=etype,
        )


def _card_section_label(data: Dict[str, Any]) -> str:
    card = (data.get("data") or {}).get("card") or {}
    sections = card.get("section") or []
    if sections and isinstance(sections[0], dict):
        title = (sections[0].get("title") or sections[0].get("name") or "").strip()
        if title:
            return title
    return ""


def _sse_progress_percent(
    etype: str,
    data: Dict[str, Any],
    progress: _TurnProgress,
) -> float:
    """Map SSE events to a monotonic 0–100 pipeline percentage for MCP hosts."""
    current = progress.progress_pct

    if etype == "ai_chunk":
        return max(current, 8.0)

    if etype == "tool_call":
        return max(current, 10.0)

    if etype == "report_layout":
        return max(current, 15.0)

    if etype == "report_type":
        return max(current, 18.0)

    if etype == "card_stream_start":
        return max(current, 22.0)

    if etype == "card":
        # Spread card writes across ~22–88; exact section count is unknown upfront.
        return max(current, min(88.0, 22.0 + progress.card_count * 11.0))

    if etype == "card_generation_heartbeat":
        # Nudge forward during long section writes without jumping to completion.
        return max(current, min(89.0, current + 1.0))

    if etype == "card_stream_complete":
        return max(current, 90.0)

    if etype == "report_citations":
        return max(current, 93.0)

    if etype.startswith("learning_brain_") or etype.startswith("document_analysis_"):
        if etype.endswith("_start"):
            return max(current, 12.0)
        if etype.endswith("_end"):
            return max(current, 20.0)
        return max(current, min(25.0, current + 1.0))

    if etype == "new_chat_generation":
        return max(current, 5.0)

    if etype == _CHAT_DONE_TYPE:
        return max(current, 95.0)

    if etype == _REPORT_DONE_TYPE:
        return 100.0

    return current


def _sse_progress_message(
    etype: str,
    data: Dict[str, Any],
    progress: _TurnProgress,
) -> Optional[str]:
    """Map backend SSE ``type`` values to short host-facing status text."""
    if etype == "ai_chunk":
        if progress.chat_stream_started:
            return None
        progress.chat_stream_started = True
        return "Caspr is responding..."

    if etype == "tool_call":
        return "Starting report generation..."

    if etype == "card_stream_start":
        title = (data.get("report_title") or "").strip()
        return f"Generating report{': ' + title if title else ''}..."

    if etype == "report_layout":
        return "Planning report structure..."

    if etype == "report_type":
        report_type = (data.get("report_type") or "study").strip()
        domain = (data.get("domain_name") or "").strip()
        if domain:
            return f"Report type: {report_type} ({domain})"
        return f"Report type: {report_type}"

    if etype == "card":
        progress.card_count += 1
        section = _card_section_label(data)
        if section:
            return f"Writing section {progress.card_count}: {section}"
        return f"Writing section {progress.card_count}..."

    if etype == "card_generation_heartbeat":
        section = (data.get("section") or "").strip()
        step = (data.get("step") or "").strip()
        # DRL-authored heartbeat lines — show the step directly to the user.
        if step:
            return f"{section}: {step}" if section else step
        if section:
            return f"Still writing {section}..."
        return "Still generating report sections..."

    if etype == "card_stream_complete":
        return "Finalizing report sections..."

    if etype == "report_citations":
        return "Collecting citations..."

    if etype == "new_chat_generation":
        title = (data.get("chat_title") or "").strip()
        return f"New chat: {title}" if title else "New chat started"

    if etype.startswith("learning_brain_") or etype.startswith("document_analysis_"):
        return (data.get("message") or "").strip() or None

    if etype == _CHAT_DONE_TYPE:
        return "Saving conversation..."

    if etype == _REPORT_DONE_TYPE:
        return "Report complete"

    if etype in {"message_stream_complete"}:
        return None

    # Generic status events from the producer (start/end/heartbeat without a name).
    status = (data.get("status") or etype or "").strip()
    if status in {"start", "end", "heartbeat"}:
        return None
    return None


class CasprAPIError(Exception):
    """Raised when the caspr backend returns an error for an MCP-driven call."""

    def __init__(self, message: str, *, error_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.error_code = error_code

class CasprClient:
    """Thin async wrapper over the caspr REST + SSE API for the MCP server."""

    def __init__(self, base_url: str, auth_token: str) -> None:
        """
        Args:
            base_url: Backend API base, e.g. ``https://caspr.ai/api/v1``.
            auth_token: Caspr JWT or MCP API key (with or without the
                ``Bearer `` prefix). Both identify the same user account for
                wallet/credits as the web app.
        """
        self.base_url = base_url.rstrip("/")
        self._auth_header = self._normalize_token(auth_token)

    @staticmethod
    def _normalize_token(token: str) -> str:
        token = (token or "").strip()
        if not token:
            raise CasprAPIError("Missing caspr auth token.")
        # X-API-Key style raw keys are promoted to Bearer for the REST API.
        if token.lower().startswith("bearer "):
            return token
        return f"Bearer {token}"

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._auth_header}

    # ── Public API ────────────────────────────────────────────────────────

    async def send_message(
        self,
        message: str,
        chat_id: Optional[str] = None,
        reference_ids: Optional[List[str]] = None,
        on_progress: Optional[ProgressFn] = None,
    ) -> Dict[str, Any]:
        """Run one caspr turn end to end and return the aggregated result.

        Returns a dict:
            {
              "chat_id": str,
              "session_id": str,
              "reply": str,                 # aggregated assistant text (chat)
              "is_report_generated": bool,
              "report_id": Optional[str],
              "chat_title": Optional[str],
              "error": Optional[str],
            }
        """
        async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
            for attempt in range(2):
                force_new = attempt > 0
                try:
                    session_id, chat_id = await self._ensure_session(
                        client, chat_id, force_new=force_new
                    )
                    if on_progress:
                        await on_progress(1, "Session ready", "")
                    result = await self._run_turn(
                        client,
                        message=message,
                        chat_id=chat_id,
                        session_id=session_id,
                        reference_ids=reference_ids,
                        on_progress=on_progress,
                    )
                except CasprAPIError as exc:
                    if attempt == 0 and chat_id and self._is_session_error(exc):
                        self._forget_session(chat_id)
                        continue
                    raise

                if (
                    attempt == 0
                    and chat_id
                    and result.get("error")
                    and self._is_session_error_text(result["error"])
                ):
                    self._forget_session(chat_id)
                    continue

                if not result.get("error") and chat_id:
                    self._cache_session(chat_id, result.get("session_id") or session_id)
                return result

            raise CasprAPIError("Could not establish a valid caspr session after retry.")

    async def start_report_turn(
        self,
        message: str,
        chat_id: Optional[str] = None,
        reference_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Kick off a report turn without waiting for completion (MCP poll flow).

        Claude Desktop cancels MCP tool calls after ~60 seconds. POST /chat runs
        chat_producer in the background on the backend, so we return as soon as
        the message is accepted and poll via GET /chat/{chat_id}.
        """
        async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
            snapshot = await self._fetch_chat_snapshot(client, chat_id) if chat_id else {}

            if chat_id and snapshot.get("turn_in_progress"):
                return {
                    "chat_id": chat_id,
                    "session_id": _SESSION_BY_CHAT.get(chat_id, ""),
                    "status": "already_running",
                    "message": "A report turn is already in progress for this chat.",
                }

            if chat_id and chat_id in _ACTIVE_TURNS:
                turn = _ACTIVE_TURNS[chat_id]
                return {
                    "chat_id": chat_id,
                    "session_id": turn.get("session_id"),
                    "status": "already_running",
                    "message": "A report turn is already in progress for this chat.",
                }

            snapshot = await self._fetch_chat_snapshot(client, chat_id) if chat_id else {}
            baseline_report_ids = {
                r["id"] for r in (snapshot.get("reports") or []) if r.get("id")
            }
            baseline_message_count = len(snapshot.get("messages") or [])

            for attempt in range(2):
                force_new = attempt > 0
                session_id, chat_id = await self._ensure_session(
                    client, chat_id, force_new=force_new
                )
                try:
                    await self._post_chat(
                        client, message, chat_id, session_id, reference_ids
                    )
                except CasprAPIError as exc:
                    if exc.error_code == "TURN_IN_PROGRESS" and chat_id:
                        _ACTIVE_TURNS[chat_id] = {
                            "session_id": session_id,
                            "baseline_report_ids": baseline_report_ids,
                            "baseline_message_count": baseline_message_count,
                        }
                        return {
                            "chat_id": chat_id,
                            "session_id": session_id,
                            "status": "already_running",
                            "message": "Report generation already in progress for this chat.",
                        }
                    if attempt == 0 and self._is_session_error(exc):
                        self._forget_session(chat_id)
                        continue
                    raise

                _ACTIVE_TURNS[chat_id] = {
                    "session_id": session_id,
                    "baseline_report_ids": baseline_report_ids,
                    "baseline_message_count": baseline_message_count,
                }
                return {
                    "chat_id": chat_id,
                    "session_id": session_id,
                    "status": "started",
                }

            raise CasprAPIError("Could not establish a valid caspr session after retry.")

    async def poll_report_turn(self, chat_id: str) -> Dict[str, Any]:
        """Poll a background report turn started with ``start_report_turn``."""
        if not chat_id:
            raise CasprAPIError("chat_id is required to poll report status.")

        turn = _ACTIVE_TURNS.get(chat_id, {})
        baseline_report_ids = turn.get("baseline_report_ids", set())
        baseline_message_count = turn.get("baseline_message_count", 0)

        async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
            snapshot = await self._fetch_chat_snapshot(client, chat_id)

        reports = snapshot.get("reports") or []
        messages = snapshot.get("messages") or []
        new_reports = [r for r in reports if r.get("id") not in baseline_report_ids]

        for report in new_reports:
            status = (report.get("status") or "").lower()
            if status in _REPORT_DONE_STATUSES:
                _ACTIVE_TURNS.pop(chat_id, None)
                card_count = len(report.get("cards") or [])
                return {
                    "chat_id": chat_id,
                    "status": "report_generated",
                    "report_id": report.get("id"),
                    "report_title": report.get("title"),
                    "sections_written": card_count,
                    "latest_status": f"Report complete ({card_count} sections).",
                }
            if status == "error-generation-report":
                _ACTIVE_TURNS.pop(chat_id, None)
                return {
                    "chat_id": chat_id,
                    "status": "failed",
                    "error": "Report generation failed on the server.",
                }

        in_progress = [r for r in reports if (r.get("status") or "") == "analysis-in-progress"]
        if in_progress:
            report = in_progress[0]
            card_count = len(report.get("cards") or [])
            title = (report.get("title") or "Report").strip()
            latest = (
                f"Writing {title}: {card_count} section(s) so far..."
                if card_count
                else f"Generating {title}..."
            )
            return {
                "chat_id": chat_id,
                "status": "running",
                "report_id": report.get("id"),
                "sections_written": card_count,
                "latest_status": latest,
            }

        if len(messages) > baseline_message_count:
            new_messages = messages[baseline_message_count:]
            has_new_ai = any(m.get("type") == "ai" for m in new_messages)
            waiting_on_ai = any(m.get("type") == "human" for m in new_messages) and not has_new_ai
            if has_new_ai and not in_progress:
                reply = self._last_ai_reply(messages)
                _ACTIVE_TURNS.pop(chat_id, None)
                return {
                    "chat_id": chat_id,
                    "status": "needs_more_info",
                    "reply": reply,
                    "latest_status": "Caspr needs more information.",
                }
            if waiting_on_ai:
                return {
                    "chat_id": chat_id,
                    "status": "running",
                    "latest_status": "Caspr is thinking...",
                }

        if chat_id in _ACTIVE_TURNS:
            return {
                "chat_id": chat_id,
                "status": "running",
                "latest_status": "Report generation in progress...",
            }

        return {
            "chat_id": chat_id,
            "status": "unknown",
            "latest_status": (
                "No active report turn tracked for this chat. "
                "Call caspr_generate_report to start one."
            ),
        }

    async def get_report_download_url(
        self,
        report_id: str,
        file_type: str,
        *,
        version: int | None = None,
    ) -> str:
        """Return a presigned download URL for a specific report version."""
        async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
            resolved_version, _ = await self._resolve_version(
                client, report_id, version=version
            )
            return await self._get_version_download_url(
                client, report_id, file_type, resolved_version
            )

    async def refresh_download_link(
        self,
        report_id: str,
        file_type: str,
        version: int | None = None,
    ) -> Dict[str, Any]:
        """Return a fresh presigned URL for an existing export — no regeneration."""
        file_type = (file_type or "pdf").lower().strip()
        if file_type not in ("pdf", "md", "html", "pptx", "info_pdf"):
            raise CasprAPIError(
                'file_type must be one of "pdf", "md", "html", "pptx", or "info_pdf".'
            )

        async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
            resolved_version, _ = await self._resolve_version(
                client, report_id, version=version
            )
            if version is None:
                versions = await self._fetch_version_history(client, report_id)
                current = self._pick_current_version(versions)
                outputs = (current or {}).get("generated_outputs") or {}
                out = outputs.get(file_type) or {}
                if not out.get("generated"):
                    if file_type == "pptx":
                        hint = "Run generate_pptx first to create the PowerPoint."
                    else:
                        hint = f"Run generate_output first with output_type='{file_type}'."
                    raise CasprAPIError(
                        f"No {file_type.upper()} export exists for this report. {hint}"
                    )

            url = await self._get_version_download_url(
                client, report_id, file_type, resolved_version
            )

        return {
            "success": True,
            "download_url": url,
            "report_id": report_id,
            "file_type": file_type,
            "version": resolved_version,
            "expires_in": 3600,
            "refreshed": True,
        }

    async def generate_output(
        self,
        report_id: str,
        output_type: str = "pdf",
        *,
        version: int | None = None,
        report_version_id: str | None = None,
    ) -> Dict[str, Any]:
        """Build a report export via ``POST /generate-report``, then return its download URL.

        Uses the report's generated markdown/cards to produce pdf, md, or html.
        When ``version`` / ``report_version_id`` are omitted, the backend uses the
        active version and auto-creates a new version if cards were edited.
        """
        output_type = (output_type or "pdf").lower().strip()
        if output_type not in ("pdf", "md", "html"):
            raise CasprAPIError('output_type must be one of "pdf", "md", or "html".')

        async with httpx.AsyncClient(timeout=None) as client:
            payload: Dict[str, Any] = {
                "report_id": report_id,
                "output_type": output_type,
            }
            if report_version_id:
                payload["report_version_id"] = report_version_id
            elif version is not None:
                _, version_id = await self._resolve_version(
                    client, report_id, version=version
                )
                payload["report_version_id"] = version_id

            resp = await client.post(
                f"{self.base_url}/generate-report",
                json=payload,
                headers=self._headers,
            )
            data = self._json(resp)
            if not resp.is_success or not data.get("success"):
                raise CasprAPIError(
                    data.get("error")
                    or data.get("message")
                    or f"Report {output_type} generation failed for report {report_id}."
                )

            generated_version = data.get("version")
            if generated_version is None:
                generated_version, _ = await self._resolve_version(client, report_id)

        s3_uri_payload = data.get("s3_uri") or {}
        file_s3_uri = s3_uri_payload.get(output_type) if isinstance(s3_uri_payload, dict) else None

        download_url = await self.get_report_download_url(
            report_id, output_type, version=generated_version
        )
        return {
            "success": True,
            "message": data.get("message"),
            "download_url": download_url,
            "s3_uri": file_s3_uri,
            "output_type": output_type,
            "already_exists": data.get("already_exists"),
            "report_id": report_id,
            "report_version_id": data.get("report_version_id"),
            "version": generated_version,
        }

    async def generate_pptx(
        self,
        report_id: str,
        generation_mode: str = "template",
        *,
        version: int | None = None,
        report_version_id: str | None = None,
    ) -> Dict[str, Any]:
        """Generate a PPTX via ``POST /generate-presentation``, then return its download URL."""
        async with httpx.AsyncClient(timeout=None) as client:
            payload: Dict[str, Any] = {
                "report_id": report_id,
                "generation_mode": generation_mode,
            }
            if report_version_id:
                payload["report_version_id"] = report_version_id
            elif version is not None:
                _, version_id = await self._resolve_version(
                    client, report_id, version=version
                )
                payload["report_version_id"] = version_id

            resp = await client.post(
                f"{self.base_url}/generate-presentation",
                json=payload,
                headers=self._headers,
            )
            data = self._json(resp)
            if not resp.is_success or not data.get("success"):
                raise CasprAPIError(
                    data.get("error") or data.get("message") or f"PPTX generation failed for report {report_id}."
                )

            generated_version = data.get("version")
            if generated_version is None:
                generated_version, _ = await self._resolve_version(client, report_id)

        download_url = await self.get_report_download_url(
            report_id, "pptx", version=generated_version
        )
        return {
            "success": True,
            "message": data.get("message"),
            "download_url": download_url,
            "s3_uri": data.get("pptx_s3_uri"),
            "already_exists": data.get("already_exists"),
            "report_id": report_id,
            "report_version_id": data.get("report_version_id"),
            "version": generated_version,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    async def _fetch_version_history(
        self, client: httpx.AsyncClient, report_id: str
    ) -> List[Dict[str, Any]]:
        resp = await client.get(
            f"{self.base_url}/report-version-history/{report_id}",
            headers=self._headers,
        )
        hist = self._json(resp)
        if not resp.is_success or not hist.get("success"):
            raise CasprAPIError(
                hist.get("error")
                or f"Could not look up versions for report {report_id}."
            )
        return hist.get("versions") or []

    @staticmethod
    def _pick_current_version(
        versions: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        current = next((v for v in versions if v.get("is_current")), None)
        if not current and versions:
            current = max(versions, key=lambda v: v.get("version", 0))
        return current

    async def _resolve_version(
        self,
        client: httpx.AsyncClient,
        report_id: str,
        *,
        version: int | None = None,
        report_version_id: str | None = None,
    ) -> tuple[int, str]:
        """Return ``(version_number, report_version_id)`` for the requested or latest version."""
        versions = await self._fetch_version_history(client, report_id)
        if not versions:
            raise CasprAPIError(f"No version found for report {report_id}.")

        if report_version_id:
            match = next(
                (v for v in versions if v.get("report_version_id") == report_version_id),
                None,
            )
            if not match:
                raise CasprAPIError(
                    f"Version id {report_version_id} not found for report {report_id}."
                )
            return match["version"], report_version_id

        if version is not None:
            match = next((v for v in versions if v.get("version") == version), None)
            if not match:
                raise CasprAPIError(
                    f"Version {version} not found for report {report_id}."
                )
            return version, match["report_version_id"]

        current = self._pick_current_version(versions)
        if not current:
            raise CasprAPIError(f"No version found for report {report_id}.")
        return current["version"], current["report_version_id"]

    async def _get_version_download_url(
        self,
        client: httpx.AsyncClient,
        report_id: str,
        file_type: str,
        version: int,
    ) -> str:
        resp = await client.get(
            f"{self.base_url}/download-version",
            params={
                "report_id": report_id,
                "version": version,
                "file_type": file_type,
            },
            headers=self._headers,
        )
        data = self._json(resp)
        url = data.get("presigned_url")
        if not resp.is_success or not data.get("success") or not url:
            raise CasprAPIError(
                data.get("error")
                or f"Could not get {file_type} download link for report {report_id} v{version}."
            )
        return url

    @staticmethod
    def _is_session_error_text(text: str) -> bool:
        lowered = (text or "").lower()
        return any(
            phrase in lowered
            for phrase in (
                "session expired",
                "session has ended",
                "invalid session",
                "session mismatch",
            )
        )

    @classmethod
    def _is_session_error(cls, exc: CasprAPIError) -> bool:
        return cls._is_session_error_text(str(exc))

    @staticmethod
    def _cache_session(chat_id: str, session_id: str) -> None:
        if chat_id and session_id:
            _SESSION_BY_CHAT[chat_id] = session_id

    @staticmethod
    def _forget_session(chat_id: str) -> None:
        _SESSION_BY_CHAT.pop(chat_id, None)

    async def _ensure_session(
        self,
        client: httpx.AsyncClient,
        chat_id: Optional[str],
        *,
        force_new: bool = False,
    ) -> tuple[str, str]:
        """Return a session for this chat, reusing the MCP cache when possible."""
        if chat_id and not force_new:
            async with _SESSION_LOCK:
                cached = _SESSION_BY_CHAT.get(chat_id)
            if cached:
                logger.debug(
                    "[caspr_mcp] reusing cached session for chat_id=%s", chat_id
                )
                return cached, chat_id

        session_id, new_chat_id = await self._create_session(client, chat_id)
        self._cache_session(new_chat_id, session_id)
        return session_id, new_chat_id

    async def _fetch_chat_snapshot(
        self, client: httpx.AsyncClient, chat_id: Optional[str]
    ) -> Dict[str, Any]:
        if not chat_id:
            return {}
        resp = await client.get(
            f"{self.base_url}/chat/{chat_id}",
            headers=self._headers,
        )
        data = self._json(resp)
        if not resp.is_success or not data.get("success"):
            raise CasprAPIError(
                data.get("error") or f"Could not load chat {chat_id}."
            )
        return data

    @staticmethod
    def _last_ai_reply(messages: List[Dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("type") == "ai":
                return (msg.get("content") or "").strip()
        return ""

    async def _create_session(
        self, client: httpx.AsyncClient, chat_id: Optional[str]
    ) -> tuple[str, str]:
        resp = await client.post(
            f"{self.base_url}/create-session",
            json={"chat_id": chat_id},
            headers=self._headers,
        )
        data = self._json(resp)
        session_id = data.get("session_id")
        new_chat_id = data.get("chat_id")
        if not resp.is_success or not session_id or not new_chat_id:
            raise CasprAPIError(data.get("error") or "Failed to create caspr session.")
        return session_id, new_chat_id

    async def _run_turn(
        self,
        client: httpx.AsyncClient,
        *,
        message: str,
        chat_id: str,
        session_id: str,
        reference_ids: Optional[List[str]],
        on_progress: Optional[ProgressFn] = None,
    ) -> Dict[str, Any]:
        stream_url = f"{self.base_url}/chat-stream/{session_id}"
        last_stream_error: str | None = None

        for stream_attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as stream_client:
                    async with stream_client.stream(
                        "GET",
                        stream_url,
                        params={"chat_id": chat_id, "event_id": "0"},
                        headers={**self._headers, "Accept": "text/event-stream"},
                    ) as resp:
                        if resp.status_code == 401 and stream_attempt == 0:
                            body = await resp.aread()
                            last_stream_error = body.decode(errors="replace")
                            logger.warning(
                                "[caspr_mcp] chat-stream session mismatch; "
                                "creating a fresh session and retrying once"
                            )
                            self._forget_session(chat_id)
                            session_id, chat_id = await self._create_session(
                                client, chat_id
                            )
                            self._cache_session(chat_id, session_id)
                            stream_url = f"{self.base_url}/chat-stream/{session_id}"
                            break

                        if not resp.is_success:
                            body = await resp.aread()
                            raise CasprAPIError(
                                f"chat-stream failed ({resp.status_code}): {body!r}"
                            )

                        return await self._consume_chat_stream(
                            client,
                            resp,
                            message=message,
                            chat_id=chat_id,
                            session_id=session_id,
                            reference_ids=reference_ids,
                            on_progress=on_progress,
                        )
            except CasprAPIError:
                raise
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                logger.error(f"[caspr_mcp] transport error during turn: {e}")
                return {
                    "chat_id": chat_id,
                    "session_id": session_id,
                    "reply": "",
                    "is_report_generated": False,
                    "report_id": None,
                    "chat_title": None,
                    "error": "Connection error while talking to caspr.",
                }

        raise CasprAPIError(
            last_stream_error or "chat-stream rejected the session after retry."
        )

    async def _consume_chat_stream(
        self,
        client: httpx.AsyncClient,
        resp: httpx.Response,
        *,
        message: str,
        chat_id: str,
        session_id: str,
        reference_ids: Optional[List[str]],
        on_progress: Optional[ProgressFn] = None,
    ) -> Dict[str, Any]:
        reply_parts: List[str] = []
        report_id: Optional[str] = None
        chat_title: Optional[str] = None
        is_report = False
        error: Optional[str] = None
        turn_progress = _TurnProgress()
        session_created_seen = 0

        post_task = asyncio.create_task(
            self._post_chat(client, message, chat_id, session_id, reference_ids)
        )
        if on_progress:
            await on_progress(2, "Waiting for Caspr...", "")

        try:
            async for event_name, data in self._iter_sse(resp):
                if event_name == "error":
                    error = data.get("response") or "caspr returned an error."
                    if on_progress:
                        await on_progress(
                            turn_progress.progress_pct, f"Error: {error}", ""
                        )
                    break
                if event_name == "timeout":
                    error = "caspr session timed out."
                    if on_progress:
                        await on_progress(turn_progress.progress_pct, error, "")
                    break

                etype = data.get("type", "")
                await turn_progress.from_sse(on_progress, etype, data)
                if data.get("report_id"):
                    report_id = report_id or data["report_id"]
                if etype in _REPORT_SIGNAL_TYPES:
                    is_report = True
                if etype == "ai_chunk":
                    chunk = data.get("chunk")
                    if chunk:
                        reply_parts.append(chunk)
                elif etype == "new_chat_generation":
                    chat_title = data.get("chat_title") or chat_title
                elif etype == _REPORT_DONE_TYPE:
                    is_report = True
                    break
                elif etype == _CHAT_DONE_TYPE and not is_report:
                    break
                elif etype == _SESSION_CREATED_TYPE:
                    session_created_seen += 1
                    if session_created_seen >= 2 and not is_report:
                        break
        finally:
            await self._settle_post_task(post_task)

        return {
            "chat_id": chat_id,
            "session_id": session_id,
            "reply": "".join(reply_parts).strip(),
            "is_report_generated": is_report,
            "report_id": report_id,
            "chat_title": chat_title,
            "error": error,
        }

    async def _post_chat(
        self,
        client: httpx.AsyncClient,
        message: str,
        chat_id: str,
        session_id: str,
        reference_ids: Optional[List[str]],
    ) -> None:
        payload: Dict[str, Any] = {
            "message": message,
            "chat_id": chat_id,
            "session_id": session_id,
        }
        if reference_ids:
            payload["reference_ids"] = reference_ids
        resp = await client.post(
            f"{self.base_url}/chat",
            json=payload,
            headers=self._headers,
            timeout=_REST_TIMEOUT,
        )
        if resp.status_code == 409:
            data = self._json(resp, quiet=True)
            raise CasprAPIError(
                data.get("error") or "A turn is already in progress for this chat.",
                error_code=data.get("error_code") or "TURN_IN_PROGRESS",
            )
        if not resp.is_success:
            data = self._json(resp, quiet=True)
            raise CasprAPIError(data.get("error") or f"/chat failed ({resp.status_code}).")

    @staticmethod
    async def _settle_post_task(post_task: "asyncio.Task[None]") -> None:
        if post_task.done():
            exc = post_task.exception()
            if exc:
                if isinstance(exc, CasprAPIError) and exc.error_code == "TURN_IN_PROGRESS":
                    return
                raise exc
            return
        try:
            await asyncio.wait_for(asyncio.shield(post_task), timeout=5.0)
        except asyncio.TimeoutError:
            # Background producer is still running (report path) — that's fine,
            # the stream already told us what we needed.
            pass

    async def _iter_sse(self, resp: httpx.Response):
        """Yield ``(event_name, data_dict)`` tuples from an SSE response.

        No idle timeout — report generation can emit gaps longer than an hour
        between events. The stream ends when the server closes it or when the
        caller breaks on a completion marker.
        """
        event_name: Optional[str] = None
        data_buf: List[str] = []
        async for line in resp.aiter_lines():
            line = line.rstrip("\r")
            if line == "":
                if data_buf:
                    raw = "".join(data_buf)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"raw": raw}
                    yield event_name or "message", data
                event_name = None
                data_buf = []
                continue
            if line.startswith(":"):
                continue  # SSE comment / keep-alive
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_buf.append(line[len("data:"):].strip())

    def _json(self, resp: httpx.Response, quiet: bool = False) -> Dict[str, Any]:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            if not quiet:
                logger.error(f"[caspr_mcp] non-JSON response ({resp.status_code}) from {resp.request.url}")
            return {}
