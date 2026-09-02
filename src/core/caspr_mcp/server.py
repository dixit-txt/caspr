"""Caspr MCP server.

Exposes the *entire* caspr agent (chat, report generation with domain routing,
downloads, PPTX) to Claude or any other MCP host. Claude becomes the outer
conversational LLM and calls these tools; caspr keeps running its own graph and
LLM internally. Nothing here imports ``Casper`` — every tool talks to the
existing caspr REST/SSE API over HTTP, so auth, wallet/token billing, DB
persistence and S3 publishing are reused unchanged.

This uses the FastMCP bundled in the official ``mcp`` SDK
(``mcp.server.fastmcp``), which is already a project dependency (``mcp==1.27.0``)
— no extra packages required. The server is mounted in-process on the existing
FastAPI app at ``/mcp`` (see ``main.py``).

Design notes:
- ``caspr_chat``: one conversational turn; returns caspr's reply for the host to
  relay. If caspr asks questions, the host must ask the user — not answer itself.
- ``caspr_generate_report``: runs the full pipeline with live SSE progress in tool
  logs (default). Set ``background=true`` for start-only mode (Claude Desktop).
- ``caspr_poll_report``: check status for background-started reports only.
- ``caspr_generate_output`` / ``caspr_generate_pptx``: back the slash commands.
  These call ``POST /generate-report`` and ``POST /generate-presentation`` to
  build the file from the report markdown, then return a download URL.
- Prompts ``/generate_output`` and ``/generate_pptx`` surface as slash commands
  that drive Claude to call the matching tool.s

Auth: the caspr credential is read from the incoming request's
``Authorization`` header (set once when the platform connects). Prefer a
long-lived per-user MCP API key (``Bearer caspr_mcp_...``) created via
``POST /api/v1/mcp/api-key``; short-lived JWT access tokens still work.
For local stdio testing it falls back to the ``CASPR_AUTH_TOKEN`` env var.
The key/JWT maps to the owning Caspr account, so wallet credits and billing
follow the same path as the web app.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.config.constants import CASPR_API_BASE_URL, CASPR_MCP_ALLOWED_HOSTS
from src.config.log_helper import setup_logging
from src.core.caspr_mcp.caspr_client import CasprAPIError, CasprClient, ProgressFn
from src.core.caspr_mcp.download_labels import (
    build_download_markdown,
    resolve_download_label,
    resolve_download_label_for_url,
)

logger = setup_logging(__name__)

# ── MCP server config ────────────────────────────────────────────────────────
# CASPR_API_BASE_URL / CASPR_MCP_ALLOWED_HOSTS come from constants (.env).
# Everything else is fixed here.
CASPR_MCP_ALLOWED_ORIGINS: str | None = None
CASPR_MCP_DNS_REBIND_PROTECTION = True
CASPR_MCP_JSON_RESPONSE = False
CASPR_AUTH_TOKEN = ""
CASPR_MCP_TRANSPORT = "stdio"


def _export_download_fields(
    result: Dict[str, Any],
    *,
    file_type: str,
) -> Dict[str, str]:
    """Build friendly download label + markdown link from an export tool result."""
    url = result["download_url"]
    label = resolve_download_label_for_url(
        url,
        s3_uri=result.get("s3_uri"),
        file_type=file_type,
        version=result.get("version"),
    )
    return {
        "download_label": label,
        "download_markdown": build_download_markdown(label, url),
    }


_EXPORT_LABELS: Dict[str, str] = {
    "pdf": "PDF",
    "md": "Markdown",
    "html": "HTML",
    "pptx": "PPTX",
    "info_pdf": "visual brief PDF",
}

_T = TypeVar("_T")


def _export_wait_message(file_type: str, *, phase: str = "start") -> str:
    """User-facing status while an export is being built."""
    label = _EXPORT_LABELS.get(file_type, file_type.upper())
    if phase == "start":
        return f"Generating {label} — please wait, this may take a minute…"
    if phase == "wait":
        return f"Still generating {label}… please wait."
    return f"{label} ready."


async def _run_with_export_progress(
    ctx: Context,
    file_type: str,
    work: Callable[[], Awaitable[_T]],
    *,
    heartbeat_seconds: float = 20.0,
) -> _T:
    """Emit start + periodic wait messages while a long export API call runs."""
    await ctx.info(_export_wait_message(file_type, phase="start"))

    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_seconds)
                break
            except asyncio.TimeoutError:
                if stop.is_set():
                    break
                try:
                    await ctx.info(_export_wait_message(file_type, phase="wait"))
                except Exception:
                    logger.debug("[caspr_mcp] export heartbeat ctx.info failed", exc_info=True)

    hb_task = asyncio.create_task(_heartbeat())
    try:
        return await work()
    finally:
        stop.set()
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# DNS-rebinding protection. Enabled by default with caspr.ai + localhost
# pre-listed. Override the allow-lists with CASPR_MCP_ALLOWED_HOSTS /
# CASPR_MCP_ALLOWED_ORIGINS (comma-separated; ":*" is a port wildcard), or turn
# protection off entirely with CASPR_MCP_DNS_REBIND_PROTECTION=false (e.g. if a
# reverse proxy rewrites the Host header to an internal value).
_DEFAULT_ALLOWED_HOSTS = [
    "caspr.ai", "caspr.ai:*",
    "testcasprbackend.ezlab.in", "testcasprbackend.ezlab.in:*",
    "localhost", "localhost:*",
    "127.0.0.1", "127.0.0.1:*",
]
_DEFAULT_ALLOWED_ORIGINS = [
    "https://caspr.ai",
    "https://claude.ai",
    "https://claude.com",
]
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=CASPR_MCP_DNS_REBIND_PROTECTION,
    allowed_hosts=_csv(CASPR_MCP_ALLOWED_HOSTS) or _DEFAULT_ALLOWED_HOSTS,
    allowed_origins=_csv(CASPR_MCP_ALLOWED_ORIGINS) or _DEFAULT_ALLOWED_ORIGINS,
)

_INSTRUCTIONS = """
# Caspr MCP Server

Caspr is an AI research analyst that gathers requirements, researches, and
produces full formatted reports (with domain routing for primary research and
due diligence), plus PDF/Markdown/PPTX exports.

## How to act
- You are the conversation layer. Delegate the real work to caspr's tools.
- For normal conversation and requirement gathering, call `caspr_chat` with the
  user's message and relay caspr's reply to the user.
- When caspr asks questions (e.g. to gather requirements, clarify scope, or
  confirm details), ask those questions to the user verbatim — do NOT answer them
  yourself. Wait for the user's response, then send that response back to caspr
  via `caspr_chat` (passing the same `chat_id`).
- When the user wants a report, call `caspr_generate_report`. It runs the full
  pipeline and streams live progress (section heartbeats, status lines) via tool
  logs while it works — relay those updates to the user as they arrive. When it
  finishes, tell the user to use `/generate_output` or `/generate_pptx`. The tool
  returns a `report_id` — NOT the report text. Do NOT reproduce or summarize it.
  Use `caspr_poll_report(chat_id)` only to check on a report that was started
  with `background=true` or to get a status update later.
- Always pass the `chat_id` returned by a previous call back into the next call
  to keep the same caspr conversation.

## Exports (slash commands)
- `/generate_output` → call `caspr_generate_output(report_id, output_type)` which
  runs POST /generate-report (builds pdf/md/html from the report markdown) and
  returns a download URL. Omit `version` to use the latest active version (after
  card edits the backend auto-creates a new version). Pass `version` only when
  the user explicitly asks for an older export.
- `/generate_pptx` → call `caspr_generate_pptx(report_id)` which runs
  POST /generate-presentation and returns a PPTX download URL.
- `/refresh_download` → call `caspr_refresh_download(report_id, file_type)` when a
  download link has expired (~1 hour). This only fetches a new presigned URL for
  an existing file — it does NOT regenerate the report or rebuild exports.
- While generating PDF, Markdown, HTML, or PPTX exports, tell the user to wait
  and relay any "Generating … please wait" status lines as they arrive.
- Present exports using `download_markdown` from the tool response (e.g.
  `[Report Title.pdf](url)`). Do NOT paste the raw presigned URL. The
  `download_url` field is for the link target only — never show it as plain text.
"""

# ``streamable_http_path="/"`` so that when mounted at ``/mcp`` the endpoint is
# exactly ``/mcp`` (not ``/mcp/mcp``).
#
# json_response=False (default) streams tool-call notifications over SSE so Cursor
# can show live ctx.info / report_progress updates. With json_response=True those
# notifications are buffered until the tool finishes and never reach the client.
# Set CASPR_MCP_JSON_RESPONSE=true only if a host breaks on SSE tool responses
# (_McpClientCompatMiddleware normalizes Accept headers for Cursor either way).

caspr_server = FastMCP(
    "caspr",
    instructions=_INSTRUCTIONS,
    streamable_http_path="/",
    transport_security=_transport_security,
    stateless_http=True,
    json_response=CASPR_MCP_JSON_RESPONSE,
)


class _McpClientCompatMiddleware:
    """Normalize Accept headers for MCP hosts that omit required values.

    Streamable HTTP expects GET → ``text/event-stream``, POST → both JSON + SSE.
    Cursor and other clients sometimes send incomplete Accept headers, which yields
    406 and shows as ``{"error":"Not connected"}`` in the IDE.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        headers = list(scope.get("headers", []))
        accept_val = ""
        for key, val in headers:
            if key.lower() == b"accept":
                accept_val = val.decode()
                break

        parts = [p.strip() for p in accept_val.split(",") if p.strip()]
        has_json = any(p.startswith("application/json") for p in parts)
        has_sse = any(p.startswith("text/event-stream") for p in parts)
        updated = not accept_val

        if method == "POST" and not has_json:
            parts.append("application/json")
            updated = True
        if method in ("GET", "POST") and not has_sse:
            parts.append("text/event-stream")
            updated = True

        if updated:
            new_accept = ", ".join(dict.fromkeys(parts))
            headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            headers.append((b"accept", new_accept.encode()))
            scope = {**scope, "headers": headers}

        async def send_with_sse_headers(message):
            # Tell nginx/CDN not to buffer — required for live MCP tool progress in prod.
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = (
                    (b"x-accel-buffering", b"no"),
                    (b"cache-control", b"no-cache, no-transform"),
                )
                existing = {k.lower() for k, _ in headers}
                for key, val in extra:
                    if key not in existing:
                        headers.append((key, val))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_sse_headers)


def mcp_asgi_app():
    """ASGI app for ``app.mount("/mcp", ...)`` with client compatibility middleware."""
    return _McpClientCompatMiddleware(caspr_server.streamable_http_app())


# ── Auth helper ────────────────────────────────────────────────────────────

def _get_client(ctx: Context) -> CasprClient:
    """Build a CasprClient using the caller's MCP API key or JWT from the request."""
    token = ""
    try:
        request = ctx.request_context.request
        if request is not None:
            token = (
                request.headers.get("authorization")
                or request.headers.get("x-api-key")
                or ""
            )
    except Exception:  # not in an HTTP request context (e.g. stdio)
        token = ""
    if not token:
        token = CASPR_AUTH_TOKEN
    if not token:
        raise ValueError(
            "Missing caspr authorization. Connect the MCP server with an "
            "'Authorization: Bearer caspr_mcp_<your_api_key>' header "
            "(create one via POST /api/v1/mcp/api-key while logged in), "
            "or set CASPR_AUTH_TOKEN for local stdio testing."
        )
    return CasprClient(base_url=CASPR_API_BASE_URL, auth_token=token)


def _make_mcp_progress(ctx: Context) -> Tuple[ProgressFn, List[str], List[str]]:
    """Bridge Caspr SSE milestones to MCP logging + progress notifications."""
    progress_log: List[str] = []
    heartbeat_log: List[str] = []

    async def on_progress(percent: float, message: str, event_type: str = "") -> None:
        if not message:
            return
        progress_log.append(message)
        if event_type == "card_generation_heartbeat":
            heartbeat_log.append(message)
        logger.info(f"[caspr_mcp] {percent:.0f}% — {message}")

        try:
            await ctx.info(message)
        except Exception:
            logger.debug("[caspr_mcp] ctx.info failed", exc_info=True)

        try:
            meta = ctx.request_context.meta
            progress_token = getattr(meta, "progressToken", None) if meta else None
            if progress_token is None:
                return
            # Streamable HTTP needs related_request_id on the POST SSE stream.
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=percent,
                total=100,
                message=message,
                related_request_id=ctx.request_id,
            )
        except Exception:
            logger.debug("[caspr_mcp] progress notification failed", exc_info=True)

    return on_progress, progress_log, heartbeat_log


def _progress_fields(
    progress_log: List[str],
    heartbeat_log: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not progress_log:
        return {}
    fields: Dict[str, Any] = {
        "progress_log": progress_log,
        "latest_status": progress_log[-1],
    }
    if heartbeat_log:
        fields["heartbeat_log"] = heartbeat_log
    return fields


async def _run_caspr_turn(
    ctx: Context,
    client: CasprClient,
    *,
    message: str,
    chat_id: Optional[str] = None,
    label: str = "Caspr",
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    on_progress, progress_log, heartbeat_log = _make_mcp_progress(ctx)
    start_msg = f"{label}: starting..."
    progress_log.append(start_msg)
    logger.info(f"[caspr_mcp] {start_msg}")
    await ctx.info(start_msg)
    try:
        result = await client.send_message(
            message=message,
            chat_id=chat_id,
            on_progress=on_progress,
        )
    except CasprAPIError as e:
        await ctx.error(str(e))
        raise ValueError(str(e)) from e

    if result.get("error"):
        await ctx.error(result["error"])
        raise ValueError(result["error"])

    if result.get("is_report_generated"):
        report_id = result.get("report_id") or "unknown"
        done_msg = f"{label}: report ready (report_id={report_id})"
        progress_log.append(done_msg)
        await ctx.info(done_msg)
    else:
        done_msg = f"{label}: turn complete"
        progress_log.append(done_msg)
        await ctx.info(done_msg)
    return result, progress_log, heartbeat_log


# ═══════════════════════════════════════════════════════════════════════════
#  TOOLS
# ═══════════════════════════════════════════════════════════════════════════


@caspr_server.tool(
    name="caspr_chat",
    description=(
        "Send one conversational message to caspr and get its reply. Use this for "
        "normal conversation and requirement gathering. Relay caspr's reply to the "
        "user. If caspr asks questions, present them to the user and wait for their "
        "answer — do NOT answer caspr's questions yourself; send the user's answer "
        "back in the next call. If caspr generated a report during this turn, the "
        "result includes is_report_generated=true and a report_id — in that case tell "
        "the user the report is ready and to use /generate_output or /generate_pptx. "
        "Pass the returned chat_id back on the next call to continue the same chat."
    ),
)
async def caspr_chat(
    ctx: Context,
    message: str,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    client = _get_client(ctx)
    result, progress_log, heartbeat_log = await _run_caspr_turn(
        ctx, client, message=message, chat_id=chat_id, label="caspr_chat"
    )

    response: Dict[str, Any] = {
        "chat_id": result["chat_id"],
        "reply": result["reply"],
        "is_report_generated": result["is_report_generated"],
        **_progress_fields(progress_log, heartbeat_log),
    }
    if not result.get("is_report_generated"):
        response["instructions_for_assistant"] = (
            "Present caspr's reply to the user. If it contains questions, ask them "
            "to the user and wait for their answer — do NOT answer on the user's "
            "behalf. Send the user's answer back via caspr_chat with the same chat_id."
        )
    if result.get("chat_title"):
        response["chat_title"] = result["chat_title"]
    if result.get("is_report_generated"):
        response["report_id"] = result.get("report_id")
        response["note"] = (
            "A report was generated. Do not reproduce it. Tell the user it's ready "
            "and to use /generate_output (PDF/Markdown) or /generate_pptx (PowerPoint)."
        )
    return response


@caspr_server.tool(
    name="caspr_generate_report",
    description=(
        "Run caspr's full report pipeline (requirements, research, cards, "
        "visualizations). By default this blocks until done and streams live "
        "progress in tool logs — relay status lines to the user as they arrive. "
        "Returns a short status and report_id (NOT report content). Tell the user "
        "to use /generate_output or /generate_pptx when ready. If caspr needs more "
        "info, the result contains 'reply' — ask that to the user. Pass chat_id "
        "back on follow-up calls. Set background=true only for hosts with a ~60s "
        "tool timeout (e.g. Claude Desktop); then poll with caspr_poll_report."
    ),
)
async def caspr_generate_report(
    ctx: Context,
    message: str,
    chat_id: Optional[str] = None,
    background: bool = False,
) -> Dict[str, Any]:
    client = _get_client(ctx)

    if background:
        start_msg = "caspr_generate_report: starting in background..."
        logger.info(f"[caspr_mcp] {start_msg}")
        await ctx.info(start_msg)
        try:
            start = await client.start_report_turn(message=message, chat_id=chat_id)
        except CasprAPIError as e:
            await ctx.error(str(e))
            raise ValueError(str(e)) from e

        chat_id = start["chat_id"]
        if start.get("status") == "already_running":
            await ctx.info("Report generation already in progress — poll for status.")
            return {
                "status": "already_running",
                "chat_id": chat_id,
                "session_id": start.get("session_id"),
                "instructions_for_assistant": (
                    "Call caspr_poll_report with this chat_id. Do NOT run shell sleep."
                ),
            }

        await ctx.info("Report generation started on the server (background mode).")
        return {
            "status": "started",
            "chat_id": chat_id,
            "session_id": start.get("session_id"),
            "message": "Report generation started in background.",
            "instructions_for_assistant": (
                "Call caspr_poll_report with this chat_id until report_generated "
                "or needs_more_info. Do NOT run shell sleep commands."
            ),
        }

    result, progress_log, heartbeat_log = await _run_caspr_turn(
        ctx, client, message=message, chat_id=chat_id, label="caspr_generate_report"
    )

    if result.get("is_report_generated"):
        return {
            "status": "report_generated",
            "chat_id": result["chat_id"],
            "report_id": result.get("report_id"),
            "chat_title": result.get("chat_title"),
            "message": (
                "Your report has been generated. Use /generate_output to download it "
                "as PDF or Markdown, or /generate_pptx for a PowerPoint."
            ),
            "instructions_for_assistant": (
                "Do NOT reproduce or summarize the report. Present the message above "
                "to the user. You should have relayed live progress lines during "
                "generation — share latest_status if anything was missed."
            ),
            **_progress_fields(progress_log, heartbeat_log),
        }

    return {
        "status": "needs_more_info",
        "chat_id": result["chat_id"],
        "reply": result["reply"],
        "instructions_for_assistant": (
            "Caspr is asking the user a question. Present this reply verbatim and "
            "wait for their answer. Do NOT answer on the user's behalf. When the "
            "user responds, call caspr_generate_report with their answer and the "
            "same chat_id."
        ),
        **_progress_fields(progress_log, heartbeat_log),
    }


@caspr_server.tool(
    name="caspr_poll_report",
    description=(
        "Check status of a report started with caspr_generate_report(background=true). "
        "Not needed when using the default blocking generate_report call. Call with "
        "the chat_id until status is report_generated, needs_more_info, or failed. "
        "Do NOT use shell sleep between polls."
    ),
)
async def caspr_poll_report(
    ctx: Context,
    chat_id: str,
) -> Dict[str, Any]:
    client = _get_client(ctx)
    try:
        result = await client.poll_report_turn(chat_id)
    except CasprAPIError as e:
        await ctx.error(str(e))
        raise ValueError(str(e)) from e

    latest = result.get("latest_status") or result.get("status", "")
    if latest:
        await ctx.info(latest)

    status = result.get("status")
    if status == "report_generated":
        return {
            **result,
            "message": (
                "Your report has been generated. Use /generate_output to download "
                "as PDF or Markdown, or /generate_pptx for PowerPoint."
            ),
            "instructions_for_assistant": (
                "Do NOT reproduce or summarize the report. Present the message "
                "above to the user."
            ),
        }
    if status == "needs_more_info":
        return {
            **result,
            "instructions_for_assistant": (
                "Caspr is asking the user a question. Present reply verbatim and "
                "wait for their answer. Then call caspr_generate_report with the "
                "same chat_id and their answer."
            ),
        }
    if status == "failed":
        return {
            **result,
            "instructions_for_assistant": "Tell the user report generation failed.",
        }
    if status == "unknown":
        return {
            **result,
            "instructions_for_assistant": (
                "No active turn found. Call caspr_generate_report to start."
            ),
        }

    return {
        **result,
        "instructions_for_assistant": (
            f"Still running. Call caspr_poll_report again with chat_id='{chat_id}' "
            "and relay latest_status to the user. Do NOT run shell sleep commands."
        ),
    }


@caspr_server.tool(
    name="caspr_generate_output",
    description=(
        "Generate a downloadable report export (PDF, Markdown, or HTML) from the "
        "report's markdown/cards via POST /generate-report, then return a presigned "
        "download URL. Use this for /generate_output. output_type is 'pdf' (default), "
        "'md', or 'html'. Omit version to always use the latest active version — "
        "after card edits the backend auto-creates a new version. Pass version only "
        "when the user explicitly wants an older export. Present `download_markdown` "
        "to the user — do NOT show the raw presigned URL."
    ),
)
async def caspr_generate_output(
    ctx: Context,
    report_id: str,
    output_type: str = "pdf",
    version: Optional[int] = None,
) -> Dict[str, Any]:
    output_type = (output_type or "pdf").lower().strip()
    if output_type not in ("pdf", "md", "html"):
        raise ValueError('output_type must be one of "pdf", "md", or "html".')
    client = _get_client(ctx)
    try:
        result = await _run_with_export_progress(
            ctx,
            output_type,
            lambda: client.generate_output(
                report_id, output_type=output_type, version=version
            ),
        )
    except CasprAPIError as e:
        await ctx.error(str(e))
        raise ValueError(str(e)) from e
    await ctx.info(_export_wait_message(output_type, phase="done"))
    fields = _export_download_fields(result, file_type=output_type)
    version_num = result.get("version")
    return {
        "report_id": report_id,
        "output_type": output_type,
        "version": version_num,
        "download_url": result["download_url"],
        "download_label": fields["download_label"],
        "download_markdown": fields["download_markdown"],
        "already_exists": result.get("already_exists"),
        "message": result.get("message"),
        "status_message": _export_wait_message(output_type, phase="done"),
        "instructions_for_assistant": (
            f"Tell the user the export finished (version {version_num}). "
            "Show `download_markdown` exactly as returned. Do not paste the raw "
            "presigned URL. If the link later expires (~1 hour), tell the user to "
            "run `/refresh_download` with the same version — do NOT call "
            "generate_output again unless they want a new export."
        ),
    }


@caspr_server.tool(
    name="caspr_refresh_download",
    description=(
        "Get a fresh presigned download link for an existing report export. "
        "Use when a previous link expired (~1 hour). Does NOT regenerate the report "
        "or rebuild files — only signs a new URL for the file already in S3. "
        "file_type: 'pdf' (default), 'md', 'html', 'pptx', or 'info_pdf'. "
        "Present `download_markdown` to the user."
    ),
)
async def caspr_refresh_download(
    ctx: Context,
    report_id: str,
    file_type: str = "pdf",
    version: Optional[int] = None,
) -> Dict[str, Any]:
    file_type = (file_type or "pdf").lower().strip()
    if file_type not in ("pdf", "md", "html", "pptx", "info_pdf"):
        raise ValueError(
            'file_type must be one of "pdf", "md", "html", "pptx", or "info_pdf".'
        )
    client = _get_client(ctx)
    await ctx.info(f"Refreshing {_EXPORT_LABELS.get(file_type, file_type.upper())} download link — please wait…")
    try:
        result = await client.refresh_download_link(
            report_id, file_type=file_type, version=version
        )
    except CasprAPIError as e:
        await ctx.error(str(e))
        raise ValueError(str(e)) from e
    await ctx.info(f"Fresh {_EXPORT_LABELS.get(file_type, file_type.upper())} download link ready.")
    fields = _export_download_fields(result, file_type=file_type)
    return {
        "report_id": report_id,
        "file_type": file_type,
        "version": result.get("version"),
        "download_url": result["download_url"],
        "download_label": fields["download_label"],
        "download_markdown": fields["download_markdown"],
        "expires_in": result.get("expires_in", 3600),
        "refreshed": True,
        "message": (
            f"Fresh download link generated (valid for {result.get('expires_in', 3600) // 60} minutes). "
            "The report was not regenerated."
        ),
        "instructions_for_assistant": (
            "Show the user `download_markdown` exactly as returned. "
            "Do not paste the raw presigned URL. Clarify this is a refreshed link "
            "for the existing file, not a new report generation."
        ),
    }


@caspr_server.tool(
    name="caspr_generate_pptx",
    description=(
        "Generate a PowerPoint (PPTX) from the report via POST /generate-presentation, "
        "then return a presigned download URL. Use this for /generate_pptx. "
        "generation_mode is 'template' (default) or 'scratch'. Omit version to use "
        "the latest active version (auto-updated after card edits). Present "
        "`download_markdown` to the user — do NOT show the raw presigned URL."
    ),
)
async def caspr_generate_pptx(
    ctx: Context,
    report_id: str,
    generation_mode: str = "template",
    version: Optional[int] = None,
) -> Dict[str, Any]:
    generation_mode = (generation_mode or "template").lower().strip()
    if generation_mode not in ("template", "scratch"):
        generation_mode = "template"
    client = _get_client(ctx)
    try:
        result = await _run_with_export_progress(
            ctx,
            "pptx",
            lambda: client.generate_pptx(
                report_id, generation_mode=generation_mode, version=version
            ),
        )
    except CasprAPIError as e:
        await ctx.error(str(e))
        raise ValueError(str(e)) from e
    await ctx.info(_export_wait_message("pptx", phase="done"))
    fields = _export_download_fields(result, file_type="pptx")
    version_num = result.get("version")
    return {
        "report_id": report_id,
        "file_type": "pptx",
        "version": version_num,
        "download_url": result["download_url"],
        "download_label": fields["download_label"],
        "download_markdown": fields["download_markdown"],
        "already_exists": result.get("already_exists"),
        "message": result.get("message"),
        "status_message": _export_wait_message("pptx", phase="done"),
        "instructions_for_assistant": (
            f"Tell the user the PPTX finished (version {version_num}). "
            "Show `download_markdown` exactly as returned. Do not paste the raw "
            "presigned URL. If the link later expires (~1 hour), tell the user to "
            "run `/refresh_download` with file_type='pptx' and the same version."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  PROMPTS  (surface as slash commands in Claude Desktop / Cursor)
# ═══════════════════════════════════════════════════════════════════════════


@caspr_server.prompt(
    name="generate_output",
    description="Generate and download the report as PDF or Markdown (runs generate-report).",
)
def generate_output_prompt(report_id: str = "", output_type: str = "pdf") -> str:
    if not (report_id or "").strip():
        return (
            "Ask the user for their report_id (from a prior caspr_generate_report or "
            "caspr_chat result), then call `caspr_generate_output` with that report_id "
            f"and output_type='{output_type}'. Show `download_markdown` exactly as "
            "returned — do not paste the raw presigned URL."
        )
    return (
        f"Call the `caspr_generate_output` tool with report_id='{report_id}' and "
        f"output_type='{output_type}'. That tool builds the file from the report "
        f"markdown via the generate-report API. Show me the `download_markdown` "
        f"field exactly as returned (e.g. [Report Title.pdf](url)) — do not paste "
        f"the raw presigned URL."
    )


@caspr_server.prompt(
    name="refresh_download",
    description=(
        "Get a fresh download link when a previous presigned URL expired (~1 hour). "
        "Does not regenerate the report."
    ),
)
def refresh_download_prompt(report_id: str = "", file_type: str = "pdf") -> str:
    if not (report_id or "").strip():
        return (
            "Ask the user for their report_id, then call `caspr_refresh_download` with "
            f"that report_id and file_type='{file_type}'. Do NOT regenerate the report."
        )
    return (
        f"My download link for report_id='{report_id}' may have expired. "
        f"Call `caspr_refresh_download` with report_id='{report_id}' and "
        f"file_type='{file_type}'. This must NOT regenerate the report — only "
        f"fetch a new presigned URL for the existing {file_type.upper()} file. "
        f"Show me the `download_markdown` field exactly as returned."
    )


@caspr_server.prompt(
    name="generate_pptx",
    description="Generate and download a PowerPoint (runs generate-presentation).",
)
def generate_pptx_prompt(report_id: str = "") -> str:
    if not (report_id or "").strip():
        return (
            "Ask the user for their report_id, then call `caspr_generate_pptx` with "
            "that report_id. Show `download_markdown` exactly as returned."
        )
    return (
        f"Call the `caspr_generate_pptx` tool with report_id='{report_id}'. That tool "
        f"builds the PPTX via the generate-presentation API. Show me the "
        f"`download_markdown` field exactly as returned — do not paste the raw "
        f"presigned URL."
    )


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT (optional standalone run for local testing)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Caspr MCP Server (standalone)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=CASPR_MCP_TRANSPORT,
    )
    args = parser.parse_args()
    logger.info(f"Starting Caspr MCP server standalone | transport={args.transport} | api_base={CASPR_API_BASE_URL}")
    caspr_server.run(transport=args.transport)
